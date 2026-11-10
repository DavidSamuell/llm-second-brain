"""`move` (SPEC §6.4, plan D12): all-or-nothing rename with link updates."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from vault_core.fsio import file_version
from vault_core.history import HistoryStore
from vault_core.policy import Policy
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.writer import VaultWriter


def writer(vault: Path, data: Path, policy: Policy) -> VaultWriter:
    return VaultWriter(vault, data, policy, ack_timeout_s=5)


def cred(store: CredentialStore, folders: list[str] | None = None) -> Credential:
    name = f"m{len(store.list_credentials())}"
    return store.create_bearer(
        name=name, owner="agent@example.com", scope="write", folders=folders
    )[1]


def err(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc.value))


def snapshot(vault: Path) -> dict[str, bytes]:
    return {
        p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*") if p.is_file()
    }


@pytest.fixture
def pages(vault: Path) -> Path:
    (vault / "wiki" / "concepts" / "search.md").write_text(
        "# Search\n\nSee [the index](../index.md).\n"
    )
    (vault / "wiki" / "index.md").write_text(
        "---\nmaintainer: agent\n---\n# Index\n\n- [[search]]\n- [[search|Find]]\n- [[search#Ranking]]\n"
        "- ![[search]]\n- [md](concepts/search.md)\n"
    )
    (vault / "wiki" / "unrelated.md").write_text("# Unrelated\n")
    return vault


async def test_move_renames_and_updates_every_link_style(
    pages: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    w = writer(pages, data, policy)
    version = file_version(pages / "wiki/concepts/search.md")
    assert version is not None
    out = await w.move(cred(store), "wiki/concepts/search.md", "wiki/guides/finding.md", version)
    assert out["path"] == "wiki/guides/finding.md"
    assert out["updated_links_in"] == ["wiki/index.md"]
    assert not (pages / "wiki/concepts/search.md").exists()
    assert (
        pages / "wiki/guides/finding.md"
    ).read_text() == "# Search\n\nSee [the index](../index.md).\n"
    index = (pages / "wiki/index.md").read_text()
    assert (
        "- [[finding]]\n- [[finding|Find]]\n- [[finding#Ranking]]\n- ![[finding]]\n- [md](guides/finding.md)\n"
        in index
    )


async def test_move_records_history_and_audit_per_touched_file(
    pages: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    w = writer(pages, data, policy)
    await w.move(
        cred(store),
        "wiki/concepts/search.md",
        "wiki/guides/finding.md",
        file_version(pages / "wiki/concepts/search.md"),
    )  # type: ignore[arg-type]
    hist = HistoryStore(data)
    moved = [v for v in hist.versions("wiki/guides/finding.md") if v.source == "mcp"]
    assert [(v.event, v.from_path) for v in moved] == [("rename", "wiki/concepts/search.md")]
    old_head = hist.head("wiki/concepts/search.md")
    assert old_head is not None
    assert old_head.event == "rename"
    assert hist.deleted_under("wiki/") == {}  # moved, not deleted
    assert len([v for v in hist.versions("wiki/index.md") if v.source == "mcp"]) == 1
    rows = [
        json.loads(x) for x in (data / "audit.log").read_text().splitlines() if '"tool":"move"' in x
    ]
    assert sorted(r["path"] for r in rows) == ["wiki/concepts/search.md", "wiki/index.md"]
    assert len({r["group_id"] for r in rows}) == 1


@pytest.mark.parametrize(
    ("blocker_path", "blocker_text", "folders", "visible", "hidden"),
    [
        (
            "projects/alpha.md",
            "---\nmaintainer: human\n---\n[[search]]\n",
            None,
            ["projects/alpha.md"],
            0,
        ),
        ("private/notes.md", "[[search]]\n", None, [], 1),  # read.deny: count only
        (
            "projects/plan.md",
            "---\nmaintainer: agent\n---\n[[search]]\n",
            ["wiki/"],
            [],
            1,
        ),  # out of scope
        ("wiki/.draft.md", "[[search]]\n", None, [], 1),  # hidden page
    ],
)
async def test_any_unwritable_linker_blocks_the_whole_move(
    pages: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object,
    blocker_path: str, blocker_text: str, folders: list[str] | None, visible: list[str], hidden: int,
) -> None:  # fmt: skip
    (pages / blocker_path).parent.mkdir(parents=True, exist_ok=True)
    (pages / blocker_path).write_text(blocker_text)
    before = snapshot(pages)
    with pytest.raises(ToolError) as exc:
        await writer(pages, data, policy).move(
            cred(store, folders), "wiki/concepts/search.md", "wiki/guides/finding.md",
            file_version(pages / "wiki/concepts/search.md"),  # type: ignore[arg-type]
        )  # fmt: skip
    e = err(exc)
    assert e["code"] == "forbidden"
    assert (e["details"]["blocking_files"], e["details"]["hidden_blockers"]) == (visible, hidden)
    assert "review_queue_add" in e["message"]
    assert snapshot(pages) == before
    if hidden:
        assert blocker_path not in str(exc.value)


async def test_links_in_obsidian_trash_do_not_block(
    pages: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object
) -> None:
    (pages / ".trash").mkdir()
    (pages / ".trash" / "old.md").write_text("[[search]]\n")
    await writer(pages, data, policy).move(
        cred(store), "wiki/concepts/search.md", "wiki/guides/finding.md",
        file_version(pages / "wiki/concepts/search.md"),  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.mark.parametrize(
    ("dest", "base", "expected"),
    [
        ("wiki/unrelated.md", "current", "conflict"),  # destination exists
        ("wiki/guides/finding.md", "stale", "conflict"),
        ("projects/finding.md", "current", "forbidden"),  # destination outside write.allow
        ("wiki/concepts/search.md", "current", "invalid"),
    ],
)
async def test_move_refusals_change_nothing(
    pages: Path, data: Path, policy: Policy, store: CredentialStore, watcher: object,
    dest: str, base: str, expected: str,
) -> None:  # fmt: skip
    before = snapshot(pages)
    version = file_version(pages / "wiki/concepts/search.md") if base == "current" else "0" * 64
    with pytest.raises(ToolError) as exc:
        await writer(pages, data, policy).move(
            cred(store), "wiki/concepts/search.md", dest, version
        )  # type: ignore[arg-type]
    assert err(exc)["code"] == expected
    assert snapshot(pages) == before


CRASH_MID_GROUP = textwrap.dedent(
    """
    import asyncio, os, sys
    from pathlib import Path
    from vault_core import fsio
    from vault_core.policy import load_policy
    from vault_mcp.store import CredentialStore
    import vault_mcp.writer as writer_module

    vault, data, policy_file = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    real = writer_module.replace_from_temp
    calls = []
    def dying(tmp, target):
        calls.append(target)
        if len(calls) == 2:  # the link update landed; die before the moved file lands
            os._exit(9)
        real(tmp, target)
    writer_module.replace_from_temp = dying
    cred = CredentialStore(data / "tokens.db").list_credentials()[0]
    w = writer_module.VaultWriter(vault, data, load_policy(policy_file), ack_timeout_s=1)
    src = "wiki/concepts/search.md"
    asyncio.run(w.move(cred, src, "wiki/guides/finding.md", fsio.file_version(vault / src)))
    """
)


def test_vault_mcp_killed_mid_move_alerts_with_the_partly_applied_group(
    pages: Path, data: Path, policy_file: Path, policy: Policy, store: CredentialStore
) -> None:
    from vault_watcher.watcher import Watcher, WatcherConfig

    cred(store)
    (data / "vault-watcher").mkdir()
    (data / "vault-watcher" / "heartbeat").touch()
    proc = subprocess.run(
        [sys.executable, "-c", CRASH_MID_GROUP, str(pages), str(data), str(policy_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 9, proc.stderr

    class Alerts:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, Any]]] = []

        def send(
            self,
            event: str,
            severity: str,
            message: str,
            details: dict[str, Any] | None = None,
            *,
            subject: str = "",
        ) -> bool:
            self.sent.append((event, details or {}))
            return True

    alerts = Alerts()
    w = Watcher(
        WatcherConfig(vault_dir=pages, data_dir=data, heartbeat_interval_s=0.1, policy=policy),
        alerts=alerts,
    )
    w.start()
    w.stop()
    partial = [d for e, d in alerts.sent if e == "move_partial"]
    assert len(partial) == 1
    assert partial[0]["outcomes"] == ["aborted", "applied"]
    assert (pages / "wiki/concepts/search.md").exists()  # the move itself did not land


async def test_save_between_two_group_replacements_is_not_overwritten(
    pages: Path,
    data: Path,
    policy: Policy,
    store: CredentialStore,
    watcher: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import vault_mcp.writer as writer_module

    (pages / "wiki" / "other.md").write_text("---\nmaintainer: agent\n---\n[[search]]\n")
    real = writer_module.replace_from_temp
    calls: list[Path] = []

    def save_in_between(tmp: Path, target: Path) -> None:
        real(tmp, target)
        calls.append(target)
        if len(calls) == 1:  # wiki/index.md replaced; the browser now saves wiki/other.md
            (pages / "wiki" / "other.md").write_text("browser save\n")

    monkeypatch.setattr(writer_module, "replace_from_temp", save_in_between)
    with pytest.raises(ToolError) as exc:
        await writer(pages, data, policy).move(
            cred(store), "wiki/concepts/search.md", "wiki/guides/finding.md",
            file_version(pages / "wiki/concepts/search.md"),  # type: ignore[arg-type]
        )  # fmt: skip
    e = err(exc)
    assert e["code"] == "conflict"
    assert e["details"]["applied"] == ["wiki/index.md"]
    assert e["details"]["not_applied"] == ["wiki/other.md", "wiki/concepts/search.md"]
    assert (pages / "wiki" / "other.md").read_text() == "browser save\n"
    assert (pages / "wiki" / "concepts" / "search.md").exists()
    for _ in range(50):
        if any(event == "move_partial" for event, _ in watcher.alerts.sent):
            break
        await asyncio.sleep(0.1)
    assert any(event == "move_partial" for event, _ in watcher.alerts.sent)
    assert not list((pages / "wiki").rglob(".*.vault-mcp-*.tmp"))
