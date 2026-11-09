"""History is recorded by vault-watcher for edits from any writer (SPEC §7.1)."""

import json
import time
from pathlib import Path

from harness import Stack


def _pointers(stack: Stack, rel: str) -> list[dict[str, object]]:
    d = stack.root / "data" / "history" / rel
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def test_existing_files_get_a_baseline_and_a_shell_edit_a_new_version(stack: Stack) -> None:
    page: Path = stack.root / "vault" / "wiki" / "index.md"
    assert _pointers(stack, "wiki/index.md"), "startup scan recorded no baseline"
    page.write_text(page.read_text() + "\nedited from the host\n")
    deadline = time.monotonic() + 20  # 5 s debounce + margin
    while len(_pointers(stack, "wiki/index.md")) < 2:
        assert time.monotonic() < deadline, "edit was not recorded"
        time.sleep(0.2)
    assert _pointers(stack, "wiki/index.md")[-1]["source"] == "web"


def test_watcher_heartbeat_is_fresh(stack: Stack) -> None:
    beat = stack.root / "data" / "vault-watcher" / "heartbeat"
    assert time.time() - beat.stat().st_mtime < 30
