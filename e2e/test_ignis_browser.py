"""Nightly browser check of the pinned Ignis (SPEC §16.12, plan D38): a note open in the editor
shows an agent's edit, and the person's next typing is saved on top of it, not over it."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastmcp import Client
from harness import Stack

pytestmark = pytest.mark.nightly
playwright = pytest.importorskip(
    "playwright.sync_api", reason="nightly only: uv sync --group nightly"
)


def test_16_12_open_note_shows_agent_edit_and_keeps_both(stack: Stack) -> None:
    token = stack.token(
        "create", "--name", "nightly-agent", "--owner", "n@example.com", "--scope", "write"
    )
    note = stack.root / "vault" / "wiki" / "index.md"
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(stack.ignis_url + "/", wait_until="domcontentloaded")
        page.get_by_text("Browse vault in Restricted Mode").click(timeout=120_000)
        page.locator(".nav-folder-title", has_text="wiki").click()
        page.locator(".nav-file-title", has_text="index").click()
        editor = page.locator(".workspace-leaf.mod-active .cm-content")
        editor.wait_for(timeout=30_000)

        async def agent_append() -> None:
            async with Client(f"{stack.mcp_url}/mcp", auth=token) as c:
                await c.call_tool(
                    "append", {"path": "wiki/index.md", "text": "\nline from the agent\n"}
                )

        # Playwright's sync API owns this thread's event loop: call MCP from another thread.
        with ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, agent_append()).result(timeout=60)
        page.wait_for_function(
            "() => document.querySelector('.workspace-leaf.mod-active .cm-content').innerText.includes('line from the agent')",
            timeout=10_000,
        )
        editor.click()
        page.keyboard.press("Control+End")
        page.keyboard.type("\nline from the person")
        deadline = time.monotonic() + 15
        while "line from the person" not in note.read_text():
            assert time.monotonic() < deadline, "the editor never saved the person's typing"
            time.sleep(0.5)
        browser.close()
    text = note.read_text()
    assert "line from the agent" in text  # not overwritten by the editor's autosave
    assert text.index("line from the agent") < text.index("line from the person")
