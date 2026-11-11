"""`_system/review-queue.md`: one Obsidian task per proposed change (SPEC §6.4, plan D15).

`- [ ] <UTC time> [[page]] — issue — suggestion — by <name> (<owner>) #review ^rq-<id>`
Humans resolve an item by ticking it; secret-scan items are tagged `#review/high`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

QUEUE_PATH = "_system/review-queue.md"
HEADER = "# Review queue\n\nTick an item once it is handled. Agents add items here instead of editing pages they may not change.\n\n"
_UNSAFE = re.compile(r"\[\[|\]\]|\s+")


def _clean(text: str) -> str:
    """One line, no wikilink brackets: field text can't break the task line or add links."""
    return _UNSAFE.sub(lambda m: " " if m.group(0).isspace() else "", text).strip()


def format_item(
    page: str,
    issue: str,
    suggestion: str,
    *,
    by: str,
    owner: str,
    ts: datetime,
    item_id: str,
    priority: Literal["normal", "high"] = "normal",
) -> str:
    link = page[:-3] if page.endswith(".md") else page
    tag = "#review/high" if priority == "high" else "#review"
    when = ts.strftime("%Y-%m-%d %H:%MZ")
    return (
        f"- [ ] {when} [[{_clean(link)}]] — {_clean(issue)} — {_clean(suggestion)}"
        f" — by {_clean(by)} ({_clean(owner)}) {tag} ^rq-{item_id.lower()}\n"
    )


def add_item(current: str | None, item: str) -> str:
    if current is None:
        return HEADER + item
    return current + ("" if not current or current.endswith("\n") else "\n") + item
