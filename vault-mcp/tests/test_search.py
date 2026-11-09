import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from vault_core.config import Settings
from vault_core.policy import Policy
from vault_core.search_index import SearchIndex
from vault_mcp.server import build_server
from vault_mcp.store import CredentialStore
from vault_mcp.tools_search import SearchTools


@pytest.fixture
def indexed(vault: Path, data: Path, policy: Policy) -> Path:
    (vault / "wiki" / "concepts" / "search.md").write_text("# Search\n\nThe quarry keyword.\n")
    (vault / "projects" / "alpha.md").write_text("# Alpha\n\nquarry in a project\n")
    (vault / "private" / "salaries.md").write_text("# Pay\n\nquarry but private\n")
    SearchIndex(data / "index.db", policy).rebuild(vault)
    return data / "index.db"


def cred(store: CredentialStore, folders: list[str] | None = None):  # type: ignore[no-untyped-def]
    name = f"s{len(store.list_credentials())}"
    return store.create_bearer(name=name, owner="s@example.com", scope="read", folders=folders)[1]


def paths(out: dict) -> list[str]:  # type: ignore[type-arg]
    return sorted(r["path"] for r in out["results"])


def test_search_never_returns_denied_paths(
    indexed: Path, policy: Policy, store: CredentialStore
) -> None:
    out = SearchTools(indexed, policy).search(cred(store), "quarry")
    assert paths(out) == ["projects/alpha.md", "wiki/concepts/search.md"]
    assert set(out["results"][0]) == {"path", "title", "snippet", "score"}


def test_search_respects_credential_folders(
    indexed: Path, policy: Policy, store: CredentialStore
) -> None:
    out = SearchTools(indexed, policy).search(cred(store, ["wiki/"]), "quarry")
    assert paths(out) == ["wiki/concepts/search.md"]


def test_folder_argument_narrows_within_scope(
    indexed: Path, policy: Policy, store: CredentialStore
) -> None:
    t = SearchTools(indexed, policy)
    assert paths(t.search(cred(store), "quarry", folder="projects")) == ["projects/alpha.md"]
    assert paths(t.search(cred(store, ["wiki/concepts/"]), "quarry", folder="wiki/")) == [
        "wiki/concepts/search.md"
    ]


@pytest.mark.parametrize(
    ("folders", "folder", "code"),
    [
        (None, "private", "forbidden"),
        (["wiki/"], "projects", "forbidden"),
        (None, "/wiki", "invalid"),
    ],
)
def test_folder_argument_errors(
    indexed: Path,
    policy: Policy,
    store: CredentialStore,
    folders: list[str] | None,
    folder: str,
    code: str,
) -> None:
    with pytest.raises(ToolError) as exc:
        SearchTools(indexed, policy).search(cred(store, folders), "quarry", folder=folder)
    assert json.loads(str(exc.value))["code"] == code


@pytest.mark.parametrize(
    ("query", "limit"), [("", 20), ("***", 20), ("quarry", 0), ("quarry", 101)]
)
def test_invalid_query_or_limit(
    indexed: Path, policy: Policy, store: CredentialStore, query: str, limit: int
) -> None:
    with pytest.raises(ToolError) as exc:
        SearchTools(indexed, policy).search(cred(store), query, limit=limit)
    assert json.loads(str(exc.value))["code"] == "invalid"


def test_missing_index_is_reported(data: Path, policy: Policy, store: CredentialStore) -> None:
    with pytest.raises(ToolError) as exc:
        SearchTools(data / "index.db", policy).search(cred(store), "quarry")
    assert json.loads(str(exc.value))["code"] == "not_found"


async def test_search_tool_over_mcp(
    indexed: Path, vault: Path, data: Path, policy_file: Path, store: CredentialStore
) -> None:
    token, _ = store.create_bearer(
        name="local", owner="ops@example.com", scope="read", folders=["projects/"]
    )
    settings = Settings(vault_dir=vault, data_dir=data, policy_file=policy_file)
    async with Client(build_server(settings, stdio_token=token)) as client:
        out = (await client.call_tool("search", {"query": "quarry"})).structured_content
    assert out is not None
    assert [r["path"] for r in out["results"]] == ["projects/alpha.md"]


def test_scope_is_applied_before_the_limit(
    vault: Path, data: Path, policy: Policy, store: CredentialStore
) -> None:
    for i in range(3):  # stronger matches outside the credential's folder
        (vault / "projects" / f"q{i}.md").write_text("# Quarry\n\nquarry quarry\n")
    (vault / "wiki" / "weak.md").write_text("# Other\n\none quarry mention\n")
    SearchIndex(data / "index.db", policy).rebuild(vault)
    out = SearchTools(data / "index.db", policy).search(cred(store, ["wiki/"]), "quarry", limit=1)
    assert paths(out) == ["wiki/weak.md"]
