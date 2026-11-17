"""Source lifecycle (SPEC §6.4, §16.11, plan D14)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from vault_core import frontmatter
from vault_core.policy import Policy
from vault_mcp.sources import SourceTools
from vault_mcp.store import Credential, CredentialStore
from vault_mcp.writer import VaultWriter


@pytest.fixture
def tools(vault: Path, data: Path, policy: Policy) -> SourceTools:
    (vault / "raw").mkdir(exist_ok=True)
    return SourceTools(VaultWriter(vault, data, policy, ack_timeout_s=5))


def cred(store: CredentialStore, scope: str = "write") -> Credential:
    name = f"s{len(store.list_credentials())}"
    return store.create_bearer(name=name, owner=f"{name}@example.com", scope=scope)[1]


def err(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc.value))


def audit(data: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (data / "audit.log").read_text().splitlines()]


# --- submit_source -----------------------------------------------------------------------------


async def test_submit_writes_a_dated_slugged_page_with_provenance(
    tools: SourceTools, vault: Path, store: CredentialStore, watcher: object
) -> None:
    c = cred(store)
    out = await tools.submit_source(
        c, "Café Notes: Q3 / Plan!", content="# Notes\n\nbody\n", origin="slack #docs"
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert out["path"] == f"raw/{today}-cafe-notes-q3-plan.md"
    meta, body = frontmatter.parse((vault / out["path"]).read_text())
    assert meta is not None
    assert (meta["title"], meta["origin"], meta["submitted_by"], meta["submitted_owner"]) == (
        "Café Notes: Q3 / Plan!", "slack #docs", c.name, c.owner,
    )  # fmt: skip
    assert "url" not in meta
    assert body == "# Notes\n\nbody\n"


async def test_name_collisions_get_a_numeric_suffix(
    tools: SourceTools, store: CredentialStore, watcher: object
) -> None:
    c = cred(store)
    first = await tools.submit_source(c, "Same", content="a", origin="x")
    second = await tools.submit_source(c, "Same", content="b", origin="x")
    assert second["path"] == first["path"].replace(".md", "-2.md")


async def test_url_only_submission_is_a_stub_and_nothing_is_fetched(
    tools: SourceTools,
    vault: Path,
    store: CredentialStore,
    watcher: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("submit_source must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    out = await tools.submit_source(
        cred(store), "Spec", url="https://example.com/spec", origin="web"
    )
    meta, body = frontmatter.parse((vault / out["path"]).read_text())
    assert meta is not None
    assert meta["url"] == "https://example.com/spec"
    assert "fetch" in body.lower()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "invalid"),  # neither content nor url
        ({"url": "file:///etc/passwd"}, "invalid"),
        ({"url": "javascript:alert(1)"}, "invalid"),
    ],
)
async def test_submit_validation(
    tools: SourceTools, store: CredentialStore, watcher: object, kwargs: dict[str, str], code: str
) -> None:
    with pytest.raises(ToolError) as exc:
        await tools.submit_source(cred(store), "T", origin="o", **kwargs)
    assert err(exc)["code"] == code


async def test_frontmatter_injection_in_title_is_inert(
    tools: SourceTools, vault: Path, store: CredentialStore, watcher: object
) -> None:
    out = await tools.submit_source(
        cred(store), "x\nmaintainer: human\n---", content="body", origin="o"
    )
    meta, _ = frontmatter.parse((vault / out["path"]).read_text())
    assert meta is not None
    assert "maintainer" not in meta


# --- claim / complete / release --------------------------------------------------------------


async def _submitted(tools: SourceTools, store: CredentialStore) -> str:
    return str(
        (await tools.submit_source(cred(store), "Doc", content="# Doc\n", origin="o"))["path"]
    )


async def test_16_11_concurrent_claims_exactly_one_wins(
    tools: SourceTools, store: CredentialStore, watcher: object
) -> None:
    path = await _submitted(tools, store)
    a, b = cred(store), cred(store)
    results = await asyncio.gather(
        tools.claim_source(a, path), tools.claim_source(b, path), return_exceptions=True
    )
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, ToolError)]
    assert len(wins) == 1
    assert len(losses) == 1
    assert json.loads(str(losses[0]))["code"] == "already_claimed"
    assert wins[0]["path"].startswith("raw/_processing/")


async def test_complete_moves_to_processed_and_only_the_holder_may(
    tools: SourceTools, vault: Path, data: Path, store: CredentialStore, watcher: object
) -> None:
    path = await _submitted(tools, store)
    holder, other = cred(store), cred(store)
    claimed = (await tools.claim_source(holder, path))["path"]
    (vault / "wiki" / "summary.md").write_text("# Summary\n")
    with pytest.raises(ToolError) as exc:
        await tools.complete_source(other, claimed, "wiki/summary.md")
    assert err(exc)["code"] == "not_claimed"
    with pytest.raises(ToolError) as exc:
        await tools.complete_source(holder, claimed, "wiki/missing.md")
    assert err(exc)["code"] == "not_found"
    out = await tools.complete_source(holder, claimed, "wiki/summary.md")
    assert out["path"].startswith("raw/_processed/")
    assert (vault / out["path"]).exists()
    assert not (vault / claimed).exists()
    assert any(
        r["tool"] == "complete_source" and r.get("detail") == "summary: wiki/summary.md"
        for r in audit(data)
    )


async def test_release_returns_the_file_to_raw(
    tools: SourceTools, vault: Path, store: CredentialStore, watcher: object
) -> None:
    path = await _submitted(tools, store)
    holder = cred(store)
    claimed = (await tools.claim_source(holder, path))["path"]
    with pytest.raises(ToolError) as exc:
        await tools.release_source(cred(store), claimed)
    assert err(exc)["code"] == "not_claimed"
    out = await tools.release_source(holder, claimed)
    assert out["path"] == path
    assert (vault / path).exists()
    assert (await tools.claim_source(cred(store), path))["path"].startswith(
        "raw/_processing/"
    )  # claimable again


@pytest.mark.parametrize(
    "bad", ["raw/_processing/x.md", "raw/_processed/x.md", "wiki/index.md", "raw/sub/x.md"]
)
async def test_only_files_directly_in_raw_can_be_claimed(
    tools: SourceTools, vault: Path, store: CredentialStore, watcher: object, bad: str
) -> None:
    (vault / bad).parent.mkdir(parents=True, exist_ok=True)
    if not (vault / bad).exists():
        (vault / bad).write_text("x")
    with pytest.raises(ToolError) as exc:
        await tools.claim_source(cred(store), bad)
    assert err(exc)["code"] == "invalid"


async def test_expired_leases_return_files_to_raw(
    tools: SourceTools, vault: Path, data: Path, store: CredentialStore, watcher: object
) -> None:
    path = await _submitted(tools, store)
    claimed = (await tools.claim_source(cred(store), path, ttl_minutes=1))["path"]
    assert await tools.sweep(now=datetime.now(UTC)) == 0
    assert await tools.sweep(now=datetime.now(UTC) + timedelta(minutes=2)) == 1
    assert (vault / path).exists()
    assert not (vault / claimed).exists()
    assert any(r["tool"] == "lease_expired" for r in audit(data))


async def test_orphaned_processing_files_without_a_lease_are_returned(
    tools: SourceTools, vault: Path, store: CredentialStore, watcher: object
) -> None:
    orphan = vault / "raw" / "_processing" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("left by a crash between rename and lease")
    assert await tools.sweep(now=datetime.now(UTC)) == 0  # too recent
    assert await tools.sweep(now=datetime.now(UTC) + timedelta(hours=1)) == 1
    assert (vault / "raw" / "orphan.md").exists()


async def test_read_tokens_cannot_use_source_tools(
    tools: SourceTools, store: CredentialStore, watcher: object
) -> None:
    with pytest.raises(ToolError) as exc:
        await tools.submit_source(cred(store, "read"), "T", content="x", origin="o")
    assert err(exc)["code"] == "forbidden"
