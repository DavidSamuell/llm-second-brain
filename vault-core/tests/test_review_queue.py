from datetime import UTC, datetime

from vault_core.review_queue import HEADER, add_item, format_item

TS = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def test_item_is_a_single_obsidian_task_line_in_the_d15_format() -> None:
    line = format_item(
        "projects/alpha.md", "Out of date", "Mention the v2 API", by="alice-laptop", owner="alice@example.com",
        ts=TS, item_id="01ABC",
    )  # fmt: skip
    assert line == (
        "- [ ] 2026-11-10 09:30Z [[projects/alpha]] — Out of date — Mention the v2 API"
        " — by alice-laptop (alice@example.com) #review ^rq-01abc\n"
    )


def test_high_priority_items_are_tagged() -> None:
    line = format_item(
        "wiki/a.md",
        "Secret found",
        "Remove it",
        by="vault-watcher",
        owner="system",
        ts=TS,
        item_id="X1",
        priority="high",
    )
    assert "#review/high" in line
    assert "#review " not in line


def test_newlines_and_wikilink_brackets_cannot_break_the_line() -> None:
    line = format_item(
        "wiki/a.md", "two\nlines ]] [[x", "more\r\ntext", by="b", owner="o", ts=TS, item_id="i"
    )
    assert line.count("\n") == 1
    assert "]] [[x" not in line


def test_add_item_creates_the_queue_and_appends_on_a_new_line() -> None:
    first = add_item(None, "- [ ] one\n")
    assert first == HEADER + "- [ ] one\n"
    assert (
        add_item("# Review queue\n\n- [ ] one", "- [ ] two\n")
        == "# Review queue\n\n- [ ] one\n- [ ] two\n"
    )
