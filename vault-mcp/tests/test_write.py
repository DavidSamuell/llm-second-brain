"""`write` and `append` with the D21 handoff to a real vault-watcher (SPEC §6.4, §16.9/10/13/15)."""

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from vault_core.fsio import version_of
from vault_core.history import HistoryStore, Spool
from vault_core.policy import Policy
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.writer import VaultWriter


def writer(vault: Path, data: Path, policy: Policy, **kw: Any) -> VaultWriter:
    return VaultWriter(vault, data, policy, ack_timeout_s=kw.pop("ack_timeout_s", 5.0), **kw)


def cred(
    store: CredentialStore, scope: str = "write", folders: list[str] | None = None
) -> Credential:
    name = f"w{len(store.list_credentials())}"
    return store.create_bearer(name=name, owner="agent@example.com", scope=scope, folders=folders)[
        1
    ]


def code(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc.value))


def audit_rows(data: Path) -> list[dict[str, Any]]:
    path = data / "audit.log"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def mcp_versions(data: Path, rel: str) -> list[Any]:
    return [v for v in HistoryStore(data).versions(rel) if v.source == "mcp"]


# --- happy paths -----------------------------------------------------------------------------


async def test_create_then_replace_with_versions(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    w, c = writer(vault, data, policy), cred(store)
    created = await w.write(c, "wiki/new.md", "# New\n", base_version=None)
    assert created == {"path": "wiki/new.md", "version": version_of(b"# New\n")}
    replaced = await w.write(c, "wiki/new.md", "# New v2\n", base_version=created["version"])
    assert (vault / "wiki/new.md").read_text() == "# New v2\n"
    assert replaced["version"] == version_of(b"# New v2\n")


async def test_every_write_has_one_audit_line_and_one_history_version(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    w, c = writer(vault, data, policy), cred(store)
    version = None
    for i in range(20):  # back-to-back
        version = (await w.write(c, "wiki/burst.md", f"v{i}\n", base_version=version))["version"]
    oks = [r for r in audit_rows(data) if r["tool"] == "write" and r["result"] == "ok"]
    posts = mcp_versions(data, "wiki/burst.md")
    assert len(oks) == 20
    assert sorted(r["id"] for r in oks) == sorted(v.audit_id for v in posts)
    assert len({r["id"] for r in oks}) == 20
    assert {r["new_version"] for r in oks} == {v.sha256 for v in posts}


async def test_append_adds_a_line_boundary(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    (vault / "wiki" / "log.md").write_text("# Log\n- one")
    out = await writer(vault, data, policy).append(cred(store), "wiki/log.md", "- two\n")
    assert (vault / "wiki" / "log.md").read_text() == "# Log\n- one\n- two\n"
    assert out["version"] == version_of(b"# Log\n- one\n- two\n")


# --- refusals (nothing changes) --------------------------------------------------------------


async def test_16_9_stale_base_version_is_a_conflict_and_nothing_is_lost(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    page = vault / "wiki" / "index.md"
    old_version = version_of(page.read_bytes())
    page.write_text("edited in the browser after the agent read it\n")
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).write(
            cred(store), "wiki/index.md", "agent text\n", base_version=old_version
        )
    err = code(exc)
    assert err["code"] == "conflict"
    assert err["details"]["current_version"] == version_of(page.read_bytes())
    assert page.read_text() == "edited in the browser after the agent read it\n"


async def test_create_with_null_base_version_refuses_to_overwrite(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).write(
            cred(store), "wiki/index.md", "x", base_version=None
        )
    assert code(exc)["code"] == "conflict"


@pytest.mark.parametrize(
    ("path", "scope", "folders", "expected"),
    [
        ("projects/alpha.md", "write", None, "forbidden"),  # §16.10 maintainer: human
        ("projects/new.md", "write", None, "forbidden"),  # outside write.allow
        ("private/salaries.md", "write", None, "forbidden"),  # read.deny
        ("wiki/index.md", "read", None, "forbidden"),  # §16.5 read token
        ("wiki/index.md", "write", ["raw/"], "forbidden"),  # folder scope
        ("../escape.md", "write", None, "invalid"),
    ],
)
async def test_refused_writes_change_nothing(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object,
    path: str, scope: str, folders: list[str] | None, expected: str,
) -> None:  # fmt: skip
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).write(
            cred(store, scope, folders), path, "x", base_version=None
        )
    assert code(exc)["code"] == expected
    assert {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before


async def test_human_page_refusal_points_to_the_review_queue(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    version = version_of((vault / "projects" / "alpha.md").read_bytes())
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).write(
            cred(store), "projects/alpha.md", "x", base_version=version
        )
    assert "review_queue_add" in code(exc)["message"]


async def test_16_13_new_secrets_are_rejected_but_existing_ones_do_not_block_appends(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    fake_key = "AKIA" + "Q3RTZ7XK2P4LMNBV"
    w, c = writer(vault, data, policy), cred(store)
    with pytest.raises(ToolError) as exc:
        await w.write(c, "wiki/keys.md", f"aws key {fake_key}\n", base_version=None)
    err = code(exc)
    assert err["code"] == "secret_detected"
    assert fake_key not in str(exc.value)
    assert err["details"]["findings"] == ["aws-access-key-id at line 1"]
    (vault / "wiki" / "legacy.md").write_text(f"legacy {fake_key}\n")
    await w.append(c, "wiki/legacy.md", "a harmless line\n")


async def test_files_over_max_file_mb_are_refused(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    with pytest.raises(ToolError) as exc:  # conftest policy: max_file_mb 1
        await writer(vault, data, policy).write(
            cred(store), "wiki/big.md", "x" * (1024 * 1024 + 1), base_version=None
        )
    assert code(exc)["code"] == "too_large"


async def test_append_to_a_missing_file_is_not_found(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).append(cred(store), "wiki/typo.md", "x")
    assert code(exc)["code"] == "not_found"


# --- history handoff -------------------------------------------------------------------------


async def test_watcher_down_means_history_pending_and_no_change(
    vault: Path, data: Path, policy: Policy, store: CredentialStore
) -> None:
    (data / "vault-watcher").mkdir()
    (data / "vault-watcher" / "heartbeat").touch()
    stale = (data / "vault-watcher" / "heartbeat").stat().st_mtime - 60
    import os

    os.utime(data / "vault-watcher" / "heartbeat", (stale, stale))
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).write(cred(store), "wiki/new.md", "x", base_version=None)
    err = code(exc)
    assert (err["code"], err["details"]["applied"]) == ("history_pending", False)
    assert not (vault / "wiki" / "new.md").exists()


async def test_no_acknowledgement_reports_applied_history_pending(
    vault: Path, data: Path, policy: Policy, store: CredentialStore
) -> None:
    (data / "vault-watcher").mkdir()
    (data / "vault-watcher" / "heartbeat").touch()  # looks alive, but nothing drains the spool
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy, ack_timeout_s=0.3).write(
            cred(store), "wiki/new.md", "x", base_version=None
        )
    err = code(exc)
    assert (err["code"], err["details"]["applied"]) == ("history_pending", True)
    assert err["details"]["version"] == version_of(b"x")
    assert (vault / "wiki" / "new.md").read_text() == "x"


async def test_browser_save_right_after_an_mcp_write_keeps_both_versions(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    await writer(vault, data, policy).write(
        cred(store), "wiki/both.md", "agent\n", base_version=None
    )
    (vault / "wiki" / "both.md").write_text("browser\n")
    for _ in range(50):
        contents = [
            HistoryStore(data).content(v) for v in HistoryStore(data).versions("wiki/both.md")
        ]
        if b"browser\n" in contents:
            break
        await asyncio.sleep(0.1)
    assert contents == [b"agent\n", b"browser\n"]


CRASHING_WRITE = textwrap.dedent(
    """
    import asyncio, os, sys
    from pathlib import Path
    from vault_core import fsio, history
    from vault_core.audit import AuditLog
    from vault_core.policy import load_policy
    from vault_mcp.store import CredentialStore
    from vault_mcp.writer import VaultWriter

    vault, data, policy_file, point = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    die = lambda *a, **k: os._exit(9)
    import vault_mcp.writer as writer_module
    if point == "before_replace":
        writer_module.replace_from_temp = die
    elif point == "before_audit":
        AuditLog.append = die
    elif point == "before_commit":
        history.Spool.commit = die
    store = CredentialStore(data / "tokens.db")
    cred = store.list_credentials()[0]
    w = VaultWriter(vault, data, load_policy(policy_file), ack_timeout_s=1)
    asyncio.run(w.write(cred, "wiki/index.md", "agent text\\n", base_version=fsio.file_version(vault / "wiki/index.md")))
    """
)


@pytest.mark.parametrize(
    ("point", "file_after", "audit_results"),
    [
        ("before_replace", "original", ["aborted"]),
        ("before_audit", "agent text\n", ["ok"]),
        ("before_commit", "agent text\n", ["ok"]),
    ],
)
def test_vault_mcp_killed_mid_write_leaves_no_phantoms_and_no_missing_or_duplicate_audit(
    vault: Path, data: Path, policy_file: Path, store: CredentialStore, policy: Policy,
    point: str, file_after: str, audit_results: list[str],
) -> None:  # fmt: skip
    from vault_watcher.watcher import Watcher, WatcherConfig

    (vault / "wiki" / "index.md").write_text("original")
    cred(store)
    (data / "vault-watcher").mkdir()
    (data / "vault-watcher" / "heartbeat").touch()
    proc = subprocess.run(
        [sys.executable, "-c", CRASHING_WRITE, str(vault), str(data), str(policy_file), point],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 9, proc.stderr
    w = Watcher(
        WatcherConfig(vault_dir=vault, data_dir=data, heartbeat_interval_s=0.1, policy=policy),
        alerts=_NoAlerts(),
    )
    w.start()
    w.stop()
    assert (vault / "wiki" / "index.md").read_text() == file_after
    writes = [r for r in audit_rows(data) if r["tool"] == "write"]
    assert [r["result"] for r in writes] == audit_results
    posts = mcp_versions(data, "wiki/index.md")
    assert len(posts) == (0 if point == "before_replace" else 1)
    assert not list((vault / "wiki").glob(".*.vault-mcp-*.tmp"))


class _NoAlerts:
    def send(self, *args: object, **kwargs: object) -> bool:
        return True


async def test_write_tools_over_mcp_audit_once_per_call(
    vault: Path, data: Path, policy_file: Path, store: CredentialStore, watcher: object
) -> None:
    from fastmcp import Client

    from vault_core.config import Settings
    from vault_mcp.server import build_server

    token, _ = store.create_bearer(name="agent", owner="agent@example.com", scope="write")
    settings = Settings(vault_dir=vault, data_dir=data, policy_file=policy_file)
    async with Client(build_server(settings, stdio_token=token)) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"write", "append"} <= names
        out = (
            await client.call_tool(
                "write", {"path": "wiki/via-mcp.md", "content": "hi\n", "base_version": None}
            )
        ).structured_content
        assert out == {"path": "wiki/via-mcp.md", "version": version_of(b"hi\n")}
        with pytest.raises(ToolError):
            await client.call_tool("append", {"path": "projects/alpha.md", "text": "nope"})
    rows = [(r["tool"], r["result"]) for r in audit_rows(data)]
    assert rows == [("write", "ok"), ("append", "forbidden")]


async def test_unrecorded_browser_edit_just_before_an_mcp_write_is_kept(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    page = vault / "wiki" / "index.md"  # recorded by the watcher's startup scan
    page.write_text("browser edit still inside the debounce window\n")
    await writer(vault, data, policy).append(cred(store), "wiki/index.md", "agent line\n")
    await asyncio.sleep(0.8)  # past the debounce
    contents = [HistoryStore(data).content(v) for v in HistoryStore(data).versions("wiki/index.md")]
    assert b"browser edit still inside the debounce window\n" in contents


async def test_section_tools_edit_through_the_same_pipeline(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    page = vault / "wiki" / "guide.md"
    page.write_text("# Guide\n\n## Install\n\nold\n\n## Usage\n\nuse\n")
    w, c = writer(vault, data, policy), cred(store)
    version = version_of(page.read_bytes())
    out = await w.replace_section(c, "wiki/guide.md", "Install", "new\n", version)
    assert page.read_text() == "# Guide\n\n## Install\nnew\n## Usage\n\nuse\n"
    with pytest.raises(ToolError) as exc:  # stale version
        await w.replace_section(c, "wiki/guide.md", "Usage", "x\n", version)
    assert code(exc)["code"] == "conflict"
    await w.insert_after(c, "wiki/guide.md", "Usage", "first\n")
    assert page.read_text() == "# Guide\n\n## Install\nnew\n## Usage\nfirst\n\nuse\n"
    with pytest.raises(ToolError) as exc:
        await w.insert_after(c, "wiki/guide.md", "Missing", "x\n")
    assert code(exc)["code"] == "not_found"
    posts = mcp_versions(data, "wiki/guide.md")
    assert len(posts) == 2
    assert out["version"] == posts[0].sha256


async def test_cancelling_a_write_holds_the_lock_until_the_locked_work_has_finished(
    vault: Path,
    data: Path,
    policy: Policy,
    store: CredentialStore,
    watcher: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_module

    import vault_mcp.writer as writer_module

    real_replace = writer_module.replace_from_temp

    def slow_replace(tmp: Path, target: Path) -> None:
        time_module.sleep(0.5)  # in the worker thread, inside the locked section
        real_replace(tmp, target)

    monkeypatch.setattr(writer_module, "replace_from_temp", slow_replace)
    task = asyncio.create_task(
        writer(vault, data, policy).write(
            cred(store), "wiki/cancel.md", "landed\n", base_version=None
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The cancel must not return (and release the vault lock) while the rename is still pending.
    assert (vault / "wiki" / "cancel.md").read_text() == "landed\n"
    assert not list(Spool(data).inbox.glob("*.prepared"))


async def test_16_10_review_queue_add_works_for_a_human_page(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    w = writer(vault, data, policy)
    scoped = cred(store, folders=["wiki/", "projects/"])  # no _system/ in its folders
    out = await w.review_queue_add(scoped, "projects/alpha.md", "Outdated", "Mention v2")
    out2 = await w.review_queue_add(scoped, "projects/alpha.md", "Second", "Another")
    queue = (vault / "_system" / "review-queue.md").read_text()
    assert queue.startswith("# Review queue\n")
    lines = [line for line in queue.splitlines() if line.startswith("- [ ] ")]
    assert len(lines) == 2
    assert "[[projects/alpha]] — Outdated — Mention v2 — by" in lines[0]
    assert lines[0].endswith(f"#review ^{out['item_id']}")
    assert lines[1].endswith(f"^{out2['item_id']}")
    assert len(mcp_versions(data, "_system/review-queue.md")) == 2


@pytest.mark.parametrize(
    ("page", "scope", "expected"),
    [("private/salaries.md", "write", "forbidden"), ("projects/alpha.md", "read", "forbidden")],
)
async def test_review_queue_refusals(
    vault: Path,
    data: Path,
    policy: Policy,
    store: CredentialStore,
    watcher: object,
    page: str,
    scope: str,
    expected: str,
) -> None:
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).review_queue_add(cred(store, scope), page, "i", "s")
    assert code(exc)["code"] == expected
    assert not (vault / "_system" / "review-queue.md").exists()


async def test_writes_through_an_in_vault_alias_are_refused(
    vault: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    (vault / "wiki" / "alias.md").symlink_to(vault / "projects" / "alpha.md")
    before = (vault / "projects" / "alpha.md").read_bytes()
    with pytest.raises(ToolError) as exc:
        await writer(vault, data, policy).append(cred(store), "wiki/alias.md", "agent text\n")
    assert code(exc)["code"] == "forbidden"
    assert (vault / "projects" / "alpha.md").read_bytes() == before
