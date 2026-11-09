import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from vault_core.audit import AuditLog, new_id

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_append_writes_one_json_line_with_d20_fields(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    entry_id = log.append(
        service="vault-mcp",
        tool="write",
        result="ok",
        credential_id="c1",
        credential_name="alice-laptop",
        owner="alice@example.com",
        path="wiki/page.md",
        old_version="a" * 64,
        new_version="b" * 64,
    )
    (line,) = (tmp_path / "audit.log").read_text().splitlines()
    row = json.loads(line)
    assert row["id"] == entry_id
    assert ULID_RE.match(row["id"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", row["ts"])
    assert {
        k: row[k] for k in ("service", "tool", "result", "credential_name", "owner", "path")
    } == {
        "service": "vault-mcp",
        "tool": "write",
        "result": "ok",
        "credential_name": "alice-laptop",
        "owner": "alice@example.com",
        "path": "wiki/page.md",
    }


def test_append_keeps_a_caller_supplied_id(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    given = new_id()
    assert log.append(id=given, service="vault-watcher", tool="recover", result="aborted") == given


def test_ids_sort_by_creation_time() -> None:
    ids = [new_id() for _ in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 50


WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from vault_core.audit import AuditLog
    log, worker = AuditLog(Path(sys.argv[1])), sys.argv[2]
    for i in range(200):
        detail = worker * 4000
        log.append(service="t", tool="write", result="ok", detail=detail, path=f"{worker}/{i}")
    """
)


def test_concurrent_appends_from_processes_never_interleave(tmp_path: Path) -> None:
    target = tmp_path / "audit.log"
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(target), w]) for w in "abcd"]
    assert all(p.wait(30) == 0 for p in procs)
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == 800
    assert all(r["detail"] == r["detail"][0] * 4000 for r in rows)


def test_has_id_finds_existing_entries_only(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    assert not log.has_id("01ANY")
    entry_id = log.append(service="s", tool="t", result="ok")
    log.append(service="s", tool="t", result="ok", detail=f'mentions "id":"{entry_id}" in text')
    assert log.has_id(entry_id)
    assert not log.has_id(new_id())
