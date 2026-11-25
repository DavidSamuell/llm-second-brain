# Spec: dreaming

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Implemented; live verification on Bookbot pending · **Depends on:** scheduled-maintenance, doc-authoring, knowledge-graph

## Objective

Lint keeps the vault consistent: links resolve, pages are indexed, pages agree with each other. Dreaming keeps it correct and useful. It is a second nightly pass that looks back at what changed since the last dream, with hindsight, and:

- checks each changed page's claims against the sources it cites, and corrects or flags what doesn't match;
- merges duplicate pages, folds thin pages into the page about the same thing, and repoints links to pages that don't exist;
- keeps each touched project's `README.md` summary and status short and current, because `project_context` loads them at the start of every session;
- turns patterns that recur across sessions (the same incident, workaround or rule in two or more sources) into one stated fact on the right page.

It is the vault's version of Anthropic's "dreaming" ([The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)). Agent session transcripts are out of scope: the server never receives them. Dreaming works only from the vault, its sources and its history.

**Users:** admins (alerts, the dream report); the whole team (pages that stay correct and small).

## Behaviour

### Runner

- `scripts/maintenance --dream` (same script, token handling, tools, lock and outcome checks as the lint run; see [SPEC-scheduled-maintenance.md](SPEC-scheduled-maintenance.md)).
- Its own write credential, `maintenance-dream`, created by `scripts/maintenance --setup` (`DATA_PATH/maintenance/dream-token`, mode 600). `--dream --dry-run` uses the read-scope `maintenance-preview` credential.
- Limits: `--max-budget-usd` (default 7), timeout (default 60 min), `--max-pages` (default 40).
- Bookbot: the nightly job runs it after the lint run succeeds. A failed backup or lint skips it for that night; the pages stay in scope for the next dream.

### Scope: what changed since the last dream

The script, not the agent, decides the scope:

1. `since` = the start of the last successful dream (`DATA_PATH/maintenance/dream-since`), or 7 days ago on the first run (`--since` overrides).
2. `vault-watcher history changes --since <since> --exclude-credential maintenance-dream` lists every path with a version at or after `since`, from any source (web, agents, git, restores), leaving out versions written only by the dream itself.
3. In scope: existing Markdown pages under `domains/`, `shared/`, `wiki/` and `Home.md`. Never `_system/`, `raw/`, `skills/`, `AGENTS.md`, `CLAUDE.md`, templates or the ontology. Deleted pages are skipped.
4. Oldest change first, at most `--max-pages`. When more pages changed, the next `since` is the last change time of the first page left out, so it is picked up next night.
5. Nothing in scope: no agent runs, the log says so, and `dream-since` moves forward. No cost.

### The job

1. **Verify:** for each page in scope, read the sources it cites (`wiki/sources/` summaries and their `raw/_processed/` originals). Correct claims a source contradicts, with the citation. When a newer source about the same subject changed a fact, update the claim on non-source pages with the newer citation and keep the old fact as dated history; source summaries are never rewritten to match later events. Move unsupported claims on agent-maintained pages under `## Open questions`, marked unconfirmed. Repository citations (`repo@commit:path`) can't be opened from the vault: leave them and count them as not checkable.
2. **Consolidate** the pages in scope and their one-hop graph neighbours: merge duplicate agent-maintained wiki pages into the older one, archive the other (`status: archived`, a "Superseded by" link) and repoint its backlinks; fold thin pages the same way; repoint links to missing pages when an existing page covers the same thing.
3. **Promote:** for every project whose docs are in scope, keep its `README.md` `## Summary` within 900 characters and `## Status` within 180 (what `project_context` shows). Move detail into the project's docs and leave a link; never drop a fact.
4. **Patterns:** read the last 7 days of `_system/log.md`. When two or more sources record the same incident, workaround or rule but no runbook or wiki page states it, add it to the right page, citing every source.
5. **Report:** write `_system/dream-report.md` with `dream_run: <run id>`, the same `## Outcome` counts as the lint report, a `## Pages` line for every page in scope it finished, and a line per action. Append one `_system/log.md` line.

Rules: the vault's write policy and doc-authoring apply; `maintainer: human` pages and decision records are never edited (review item instead); no deletes; no new domains; the meaning of a page changes only on a cited source.

### Outcome and alerts

As the lint run, judged by the script through vault-mcp: agent exit and typed result, the report rewritten with this run's `dream_run:` nonce, zero errors. A failed dream posts exactly one `dream_failed` (high) alert and leaves `dream-since` unchanged, so the same changes are retried. Only a successful run (ok or unresolved) moves `dream-since`, and only past the pages its `## Pages` list says it finished: the next window starts at the first page it didn't finish. A report that finishes none of its pages is a failure. A dry run writes nothing and never moves it.

## Commands

```bash
scripts/maintenance --setup            # also creates the maintenance-dream token
scripts/maintenance --dream            # one dream (what cron calls after the lint run)
scripts/maintenance --dream --dry-run  # read-only preview, kept locally
scripts/history changes --since 2026-11-24T00:00:00Z --exclude-credential maintenance-dream
```

## Testing strategy

- Unit: `HistoryStore.changes_since` (window, deletes, excluded audit ids) and `history changes` (audit join, JSON output).
- e2e with the stub agent: a dream sees exactly the pages changed since the last dream; its own edits are not in the next scope; nothing changed means no agent and no cost; a failed dream sends one `dream_failed` alert and keeps `dream-since`; a dry run can't write and keeps `dream-since`.
- Manual (live, Bookbot): one preview and one real dream reviewed by a person.

## Boundaries

- **Always:** run after a successful backup and lint; change the vault only through MCP; alert on failure.
- **Ask first:** feeding it agent session transcripts; raising the default budget.
- **Never:** run as root; delete pages; edit rules, templates, the ontology or `maintainer: human` pages.

## Success criteria

1. A page edited during the day is in that night's dream scope; a page the dream itself edited is not in the next night's.
2. A claim that contradicts its cited source is corrected (or flagged) by morning, with a history entry under `maintenance-dream`.
3. Two agent-maintained duplicates end as one page plus an archived page whose backlinks point to the survivor.
4. A failed dream posts exactly one `dream_failed` alert, and the next night retries the same changes.
5. A night with no changes costs nothing.

## Open questions

- Session transcripts as dream input (Stage 2) need opt-in, redaction and exclusion from the GitHub snapshot; not started.
