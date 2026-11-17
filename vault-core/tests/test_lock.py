import asyncio
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from vault_core.lock import AsyncVaultLock, lock_path, read_owner, vault_lock

HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vault_core.lock import vault_lock
    data, hold, out = Path(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3])
    with vault_lock(data, "proc"):
        start = time.monotonic()
        time.sleep(hold)
        out.write_text(f"{start} {time.monotonic()}")
    """
)


def _spawn_holder(data: Path, hold: float, out: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(data), str(hold), str(out)])


def _wait_until_locked(data: Path, timeout: float = 5.0) -> None:
    """Block until some other process holds the lock (owner file written)."""
    deadline = time.monotonic() + timeout
    while read_owner(data) != "proc":
        if time.monotonic() > deadline:
            raise AssertionError("holder never took the lock")
        time.sleep(0.01)


def test_two_processes_serialize(tmp_path: Path) -> None:
    a_out, b_out = tmp_path / "a", tmp_path / "b"
    a = _spawn_holder(tmp_path, 0.4, a_out)
    _wait_until_locked(tmp_path)
    b = _spawn_holder(tmp_path, 0.1, b_out)
    assert a.wait(10) == 0
    assert b.wait(10) == 0
    a_end = float(a_out.read_text().split()[1])
    b_start = float(b_out.read_text().split()[0])
    assert b_start >= a_end, "second process entered the critical section before the first left"


def test_owner_file_names_the_current_holder(tmp_path: Path) -> None:
    with vault_lock(tmp_path, "vault-mcp"):
        assert read_owner(tmp_path) == "vault-mcp"


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not installed")
def test_python_lock_waits_for_flock_cli(tmp_path: Path) -> None:
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    cli = subprocess.Popen(["flock", str(lock_path(tmp_path)), "sleep", "0.5"])
    time.sleep(0.1)  # let flock(1) take it
    started = time.monotonic()
    with vault_lock(tmp_path, "vault-mcp"):
        waited = time.monotonic() - started
    assert cli.wait(5) == 0
    assert waited >= 0.3


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not installed")
def test_flock_cli_cannot_take_lock_held_by_python(tmp_path: Path) -> None:
    with vault_lock(tmp_path, "vault-mcp"):
        cmd = ["flock", "-n", str(lock_path(tmp_path)), "true"]
        rc = subprocess.run(cmd, check=False).returncode
    assert rc == 1


async def test_async_lock_does_not_block_the_event_loop_while_waiting(tmp_path: Path) -> None:
    holder = _spawn_holder(tmp_path, 0.4, tmp_path / "out")
    _wait_until_locked(tmp_path)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    async with AsyncVaultLock(tmp_path, "vault-mcp").hold():
        acquired_ticks = ticks
    t.cancel()
    assert holder.wait(5) == 0
    assert acquired_ticks >= 10, "event loop stalled while waiting for the flock"


async def test_async_lock_serializes_tasks_in_one_process(tmp_path: Path) -> None:
    lock = AsyncVaultLock(tmp_path, "vault-mcp")
    inside = 0
    peak = 0

    async def critical() -> None:
        nonlocal inside, peak
        async with lock.hold():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(critical() for _ in range(5)))
    assert peak == 1


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not installed")
async def test_cancelled_waiter_does_not_leave_the_lock_held(tmp_path: Path) -> None:
    holder = _spawn_holder(tmp_path, 0.4, tmp_path / "out")
    _wait_until_locked(tmp_path)
    lock = AsyncVaultLock(tmp_path, "vault-mcp")

    async def wait_for_lock() -> None:
        async with lock.hold():
            pass

    waiter = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0.1)  # waiter is now blocked behind the other process
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert holder.wait(5) == 0
    await asyncio.sleep(0.2)  # give any stray acquisition time to happen
    # Nothing in this process may still hold the flock or the in-process lock.
    cmd = ["flock", "-n", str(lock_path(tmp_path)), "true"]
    assert (await asyncio.to_thread(subprocess.run, cmd, check=False)).returncode == 0
    async with asyncio.timeout(2):
        async with lock.hold():
            pass
