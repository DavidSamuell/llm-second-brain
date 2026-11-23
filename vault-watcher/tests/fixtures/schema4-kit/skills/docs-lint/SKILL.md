---
name: docs-lint
description: Check the team docs vault - wiki and human docs (projects, specs, pipelines, runbooks) - for broken links, missing index entries, stale or uncited pages, human docs citing superseded sources, docs that disagree with the wiki, and graph problems; fix what agents may fix and report the rest. Use for scheduled or requested vault maintenance, optionally scoped to one folder.
---
# docs-lint

Keep the vault healthy, following the vault's `AGENTS.md`. When given a folder, check only pages
in it (but follow their links and edges to judge them).

## Two ways to work

- **vault-mcp tools (normal):** you have tools named `get_schema`, `search`, `list`, `read`,
  `graph_neighbors`, `graph_edges`, `backlinks`, `graph_lint`, `write`, `append`,
  `replace_section`, `insert_after`, `move`, `review_queue_add` (often prefixed, e.g.
  `mcp__vault__read`). Use them; they enforce permissions, record history, and catch conflicts.
- **Local clone (no vault-mcp):** you are in a git clone of the vault. Do the same steps on the
  files, check `maintainer` yourself before editing, and finish with one commit and push.

## Checks

1. `get_schema` and follow it. `read` `_system/graph/ontology.yaml` and
   `_system/review-queue.md` (so you don't add duplicate items).
2. `list` the folders in scope (default: `domains/` and `shared/` recursively, each `wiki/`
   subfolder, `_system/`) and `read` the pages. For large vaults, check the pages changed
   most recently first and stop after about 100 pages.
3. **Every page:** broken wikilinks; wiki page not in `_system/index.md`; orphan (nothing links
   to it); missing or invalid frontmatter (`title`, `type`, `maintainer`, `updated`); facts
   without a citation; `updated` older than 180 days; duplicates of another page; claims that
   contradict another page.
4. **Human docs** (everything under `domains/` and `shared/`):
   - not linked from its project page, or no `project:`; project page not linked from its
     domain's `README.md`, or not at `domains/<domain>/<project>/README.md`; project page without
     `repo:` when its docs cite a repo;
   - **misplaced:** its folder doesn't match its `project:` list (AGENTS.md section 2: one
     project → that project's folder, several in one domain → `domains/<domain>/shared/`,
     several domains → `shared/`);
   - no `## Change log`, or an update without a change-log line; agent working notes in the text;
     large unexplained rewrites in recent history;
   - **cites a superseded source:** collect superseded sources with
     `graph_edges(status="superseded")` (their `source`) and `graph_edges(predicate="supersedes")`
     (their object, when it is a source page). For each, `backlinks(<source summary path>)` and
     keep the human docs whose text still states a fact with that citation;
   - disagrees with the wiki: a fact in the doc differs from the current, cited fact on the wiki
     page that has `documented_in` to it; or a wiki page about the doc's subject lacks that
     `documented_in` edge.
5. **Graph:** `graph_lint(folder)` for ontology violations, missing evidence, dangling objects,
   orphan nodes and `contradicts` pairs.

## Fixes

Fix only pages you may edit: `maintainer: agent|shared` (and `maintainer: human` only to replace
a statement made obsolete by a newer cited source). Never touch `AGENTS.md`, `CLAUDE.md`,
`skills/`, `_system/templates/` or the ontology. Use `replace_section` / `insert_after` with
`base_version`, and pass `summary` (≤ 200 chars) on every human-doc edit.

- Links, index entries, frontmatter, missing project-page links and `documented_in` edges
  (only with a `source` and an `evidence` quote you read in that source page).
- **Misplaced doc:** `move` it to the folder its `project:` list requires (one move per doc; its
  links and graph references follow). If the move is refused, add a review item.
- **Superseded citation:** when the superseding source states the replacement fact, rewrite
  that statement in place citing the newer source summary, and add one change-log line:
  `- <date>: <new fact> (was <old fact>) — [[wiki/sources/<newer>]]`. If the newer source
  doesn't settle it, or the edit is refused, add a review item instead.
- **Doc vs wiki:** if the wiki's fact has the newer citation, update the doc the same way;
  otherwise add a review item.
- Graph metadata on pages you maintain: fix predicates and types to the ontology, set superseded
  edges to `status: superseded`. Never invent evidence; remove nothing a person wrote.
- Don't change meaning without a source.

On `conflict` re-read and re-apply; on `recently_edited` wait `details.retry_after_s`, re-read
and re-apply; on `forbidden` add a review item.

Everything else gets one `review_queue_add` item each (merges, contradictions, deletions,
unsettled supersessions, `maintainer: human` pages you may not edit). No duplicates.

## Report

1. Archive handled review items: `read("_system/review-queue.md")`; move every ticked line
   (`- [x] …`) unchanged to the end of `_system/review-archive.md` (create it with
   `write(..., base_version=null)` if missing, else `append`), then remove those lines from the
   queue with `write` and the queue's `base_version`. Never touch unticked lines.
2. `write("_system/lint-report.md", ...)` with the date, scope, counts per check, what you fixed
   (page and change), and which review items you added. Pass the current `base_version`.
3. `append("_system/log.md", "- <UTC time> lint <scope> — <counts> (by <your name>)\n")`.

## Local clone

Same checks on the files, reading `graph:` frontmatter directly (a source is superseded when an
edge citing it has `status: superseded` or a `supersedes` edge points to it); commit as
`docs(lint): <counts>` and push.
