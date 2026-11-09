import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from vault_core.policy import Policy
from vault_mcp.store import CredentialStore
from vault_mcp.tools_read import ReadTools


def tools(vault: Path, policy: Policy, store: CredentialStore, folders: list[str] | None = None):
    _, cred = store.create_bearer(name="t", owner="t@example.com", scope="read", folders=folders)
    return ReadTools(vault, policy), cred


def error_of(exc: pytest.ExceptionInfo[ToolError]) -> dict:
    return json.loads(str(exc.value))


def test_read_returns_content_and_sha256_version(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store)
    out = t.read(cred, "wiki/concepts/search.md")
    raw = (vault / "wiki/concepts/search.md").read_bytes()
    assert out == {
        "path": "wiki/concepts/search.md",
        "content": raw.decode(),
        "version": hashlib.sha256(raw).hexdigest(),
        "mime": "text/markdown",
    }


def test_read_binary_returns_base64_and_mime(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store)
    out = t.read(cred, "attachments/dot.png")
    assert base64.b64decode(out["content_base64"]) == (vault / "attachments/dot.png").read_bytes()
    assert out["mime"] == "image/png"
    assert "content" not in out


@pytest.mark.parametrize(
    ("path", "folders", "code"),
    [
        ("private/salaries.md", None, "forbidden"),
        (".obsidian/app.json", None, "forbidden"),
        ("projects/alpha.md", ["wiki/"], "forbidden"),
        ("../etc/passwd", None, "invalid"),
        ("wiki/missing.md", None, "not_found"),
        ("wiki", None, "invalid"),
    ],
)
def test_read_errors_use_the_d10_contract(
    vault: Path,
    policy: Policy,
    store: CredentialStore,
    path: str,
    folders: list[str] | None,
    code: str,
) -> None:
    t, cred = tools(vault, policy, store, folders)
    with pytest.raises(ToolError) as exc:
        t.read(cred, path)
    err = error_of(exc)
    assert err["code"] == code
    assert set(err) == {"code", "message", "details"}


def test_read_rejects_files_over_max_file_mb(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    (vault / "wiki" / "big.md").write_bytes(b"x" * (1024 * 1024 + 1))  # policy max_file_mb: 1
    t, cred = tools(vault, policy, store)
    with pytest.raises(ToolError) as exc:
        t.read(cred, "wiki/big.md")
    assert error_of(exc)["code"] == "too_large"


def test_list_root_hides_hidden_denied_and_shows_frontmatter_summary(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store)
    out = t.list_folder(cred, "")
    paths = [e["path"] for e in out["entries"]]
    assert paths == ["AGENTS.md", "attachments/", "notes.txt", "projects/", "wiki/"]
    assert out["truncated"] is False


def test_list_folder_reports_title_maintainer_and_tags(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store)
    out = t.list_folder(cred, "wiki")
    by_path = {e["path"]: e for e in out["entries"]}
    assert set(by_path) == {"wiki/concepts/", "wiki/index.md"}
    index = by_path["wiki/index.md"]
    assert (index["type"], index["title"], index["maintainer"], index["tags"]) == (
        "file",
        "Wiki index",
        "agent",
        ["index", "wiki"],
    )
    assert by_path["wiki/concepts/"]["type"] == "dir"


def test_list_title_falls_back_to_first_heading(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store)
    (entry,) = t.list_folder(cred, "wiki/concepts/")["entries"]
    assert (entry["title"], entry["maintainer"]) == ("Search", None)


def test_list_respects_credential_folders(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store, folders=["wiki/concepts/"])
    assert [e["path"] for e in t.list_folder(cred, "")["entries"]] == ["wiki/"]
    assert [e["path"] for e in t.list_folder(cred, "wiki/")["entries"]] == ["wiki/concepts/"]
    with pytest.raises(ToolError) as exc:
        t.list_folder(cred, "projects")
    assert error_of(exc)["code"] == "forbidden"


@pytest.mark.parametrize(
    ("folder", "code"), [("private", "forbidden"), ("nope", "not_found"), ("notes.txt", "invalid")]
)
def test_list_errors(
    vault: Path, policy: Policy, store: CredentialStore, folder: str, code: str
) -> None:
    t, cred = tools(vault, policy, store)
    with pytest.raises(ToolError) as exc:
        t.list_folder(cred, folder)
    assert error_of(exc)["code"] == code


def test_get_schema_returns_agents_md_and_schema_version(
    vault: Path, policy: Policy, store: CredentialStore
) -> None:
    t, cred = tools(vault, policy, store, folders=["raw/"])  # rules are served whatever the scope
    out = t.get_schema(cred)
    assert out["schema_version"] == 3
    assert out["content"] == (vault / "AGENTS.md").read_text()


def test_get_schema_refuses_an_agents_md_symlink_out_of_the_vault(
    tmp_path: Path, vault: Path, policy: Policy, store: CredentialStore
) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("db_password=hunter2hunter2")
    (vault / "AGENTS.md").unlink()
    (vault / "AGENTS.md").symlink_to(outside)
    t, cred = tools(vault, policy, store)
    with pytest.raises(ToolError) as exc:
        t.get_schema(cred)
    assert error_of(exc)["code"] == "forbidden"
    assert "hunter2" not in str(exc.value)


def test_list_skips_symlinks_that_escape_the_vault(
    tmp_path: Path, vault: Path, policy: Policy, store: CredentialStore
) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leak.md").write_text("---\ntitle: Leaked title\n---\n")
    (vault / "wiki" / "escape.md").symlink_to(outside_dir / "leak.md")
    (vault / "wiki" / "escape-dir").symlink_to(outside_dir)
    t, cred = tools(vault, policy, store)
    entries = t.list_folder(cred, "wiki")["entries"]
    assert [e["path"] for e in entries] == ["wiki/concepts/", "wiki/index.md"]
