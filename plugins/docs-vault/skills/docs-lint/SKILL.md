---
name: docs-lint
description: Check the team docs vault's wiki for broken links, missing index entries, stale or uncited pages and contradictions, fix what agents may fix, and report the rest. Use for scheduled or requested wiki maintenance.
---
# docs-lint

Keep the wiki healthy, following the vault's `AGENTS.md`.

## Two ways to work

- **vault-mcp tools (normal):** you have tools named `get_schema`, `search`, `list`, `read`,
  `write`, `append`, `replace_section`, `insert_after`, `move`, `submit_source`, `claim_source`,
  `complete_source`, `release_source`, `review_queue_add` (often prefixed, e.g.
  `mcp__vault__read`). Use them; they enforce permissions, record history, and catch conflicts.
- **Local clone (no vault-mcp):** you are in a git clone of the vault. Do the same steps on the
  files, check `maintainer` yourself before editing, and finish with one commit and push.
  There are no leases here, so only use this mode when no other agent ingests the same clone.

## Steps

1. `get_schema` and follow it.
2. `list` `wiki/` (each subfolder) and `_system/`; `read` the pages. For large vaults, check the
   pages changed most recently first and stop after about 100 pages.
3. Check each page for: broken wikilinks; not listed in `_system/index.md`; orphan (nothing
   links to it); missing or invalid frontmatter (`title`, `type`, `maintainer`, `updated`);
   facts without a citation; `updated` older than 180 days; duplicates of another page; claims
   that contradict another page.
4. Fix only what you maintain (`maintainer: agent|shared`, inside `wiki/` and `_system/`):
   links, index entries, frontmatter. Use `replace_section` / `insert_after` with `base_version`.
   Don't change meaning without a source.
5. Everything else gets one `review_queue_add` item each (human pages, merges, contradictions,
   deletions). Check the queue first so you don't add duplicates.
6. Archive handled review items: `read("_system/review-queue.md")`; move every ticked line
   (`- [x] …`) unchanged to the end of `_system/review-archive.md` (create it with
   `write(..., base_version=null)` if missing, else `append`), then remove those lines from the
   queue with `write` and the queue's `base_version`. Never touch unticked lines.
7. `write("_system/lint-report.md", ...)` with the date, counts per check, what you fixed, and
   which review items you added. Pass the current `base_version`.
8. `append("_system/log.md", "- <UTC time> lint — <counts> (by <your name>)\n")`.

## Local clone

Same checks on the files; commit as `docs(lint): <counts>` and push.
