"""SPEC §16.7 (search half): folder scope and read.deny apply to search."""

from fastmcp import Client
from harness import Stack


async def _search(stack: Stack, token: str, query: str) -> list[str]:
    async with Client(f"{stack.mcp_url}/mcp", auth=token) as client:
        out = (await client.call_tool("search", {"query": query})).structured_content
    assert out is not None
    return sorted(r["path"] for r in out["results"])


async def test_16_7_search_respects_deny_and_folder_scope(stack: Stack) -> None:
    everything = stack.token(
        "create", "--name", "search-all", "--owner", "e2e@example.com", "--scope", "read"
    )
    wiki_only = stack.token(
        "create",
        "--name",
        "search-wiki",
        "--owner",
        "e2e@example.com",
        "--scope",
        "read",
        "--folders",
        "wiki/",
    )
    assert await _search(stack, everything, "quarry") == ["projects/alpha.md", "wiki/index.md"]
    assert await _search(stack, wiki_only, "quarry") == ["wiki/index.md"]
