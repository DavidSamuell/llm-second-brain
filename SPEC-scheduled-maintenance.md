# Spec: scheduled-maintenance

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Implemented; Checkpoint L ready for review (automated criteria 3–4 verified, live schedule/ingest/Slack checks remain T60) · **Depends on:** doc-authoring, knowledge-graph

## Objective

Every night, after the backup, an agent on the host:

- ingests every source waiting in `raw/`;
- runs the structural and semantic health check of the whole vault;
- fixes what it can;
- reports a failure to the same Slack channel as the backup alerts.

It replaces "agent-runner `LINT_CRON`" as the recommended path for deployments that use a Claude subscription. agent-runner stays available for container-only setups.

**Users:** admins (alerts, reports); the whole team (a vault that stays correct).

## Behaviour

### Schedule and runner

- **Kit:** `scripts/maintenance` runs on the host from cron. It starts Claude Code headless (`claude -p`, the host user's login or `ANTHROPIC_API_KEY`) in a temporary working folder, with an MCP config pointing at the deployment's vault-mcp and a dedicated bearer token `maintenance`:
  - scope: write;
  - created by `scripts/maintenance --setup`;
  - stored at `DATA_PATH/maintenance/token`, mode 600; setup also creates a read-scope preview credential for dry-run, so no-write preview is enforced by the server rather than only the prompt.

  All changes go through MCP, so they get policy checks, history, audit and the secret scan.
- **Bookbot:** called at the end of the nightly job, after a successful or recovered backup, so the vault never changes before it is backed up. It also runs if the backup failed (`--after-backup-failure` notes it in the report), because maintenance doesn't depend on that night's archive.
- **Tools:** the vault MCP tools only (`mcp__vault__*`), plus `Read` confined to the temporary working folder using Claude Code restricted mode. Credential configuration is outside that folder.
- **Limits:** `--max-budget-usd` (default 5) and a wall-clock timeout (default 45 min). The cost stop uses the CLI's client-side estimate and can overshoot by an in-flight request; it is not a hard billing cap. The maintenance token's normal rate limits apply.

### The job (prompt plus the `docs-lint` and `docs-ingest` skills)

1. **Ingest:** claim, ingest and complete every file directly in `raw/`, oldest first, up to `--max-sources` (default 30). Sources it can't process are released, with a review item.
2. **Structural:**
   - broken wikilinks;
   - pages missing from `_system/index.md`;
   - orphans;
   - invalid frontmatter;
   - `graph_lint` findings (unknown predicates, missing evidence, dangling objects);
   - human docs without a project link;
   - stale `project_context` inputs (no change log on recent edits).
3. **Semantic:**
   - contradictions between pages;
   - claims superseded by newer sources;
   - facts without citations;
   - pages not updated in 180 days whose project is active;
   - duplicate pages on one topic;
   - human docs out of step with the wiki.
4. **Fix** what it may, following doc-authoring and write-policy rules (edits apply directly). File review items for what needs a person's decision.
5. **Report:** write `_system/lint-report.md` (date, counts per check, fixes made, items filed), append one `_system/log.md` line, and archive ticked review items to `_system/review-archive.md`.

### Outcome and alerts

The script decides the outcome itself; it doesn't trust the agent's word:

| Outcome | Condition | Alert (kit `scripts/lib/alert.sh`, `ALERT_WEBHOOK`) |
|---|---|---|
| ok | agent exit 0 with a typed successful result, report rewritten this run with its exact `maintenance_run:` nonce, zero declared errors and no `- error:` lines | none |
| failed | non-zero exit, timeout, budget/turn exhaustion, malformed result, unreachable MCP, rejected token, missing/stale report or nonce, missing counts, nonzero error count or `- error:` line | `maintenance_failed` high, with the reason and the log path |
| unresolved | ok, but the report lists findings it could not fix | none (they're in the review queue); a weekly summary is out of scope |

A successful dry-run instead requires a successful agent result and an unchanged report
version, and keeps its preview locally. Failed previews send the same failure alert.

Logs and the agent's final output stay under `DATA_PATH/maintenance/`. The vault report is written only through MCP. Dry-run uses the read-scope credential and produces a local preview, without changing any vault page, report, log or review archive.

## Commands

```bash
scripts/maintenance --setup                                  # create the maintenance token
scripts/maintenance --max-budget-usd 5 --max-sources 30      # one run (what cron calls)
scripts/maintenance --dry-run                                # report only, no fixes
uv run pytest e2e -k maintenance -m "profile_local or profile_none"
```

## Project structure

- `scripts/maintenance` (bash; shellcheck-clean): token handling, MCP config, the `claude -p` call with allowed tools, outcome check, alert.
- `vault-template/skills/docs-lint/SKILL.md` (semantic checks and graph lint), `vault-template/AGENTS.md` §11.
- `docs/SETUP.md` (a "Nightly maintenance" section replacing the agent-runner lint recommendation), `docs/SECURITY.md` (the maintenance token).
- Bookbot ops: `~/docs-vault-ops/nightly-backup.sh` calls `scripts/maintenance` after the backup. This is outside the kit repo.

## Code style

Same as `scripts/backup`: `set -euo pipefail`, a usage block in the header comment, `die()`, no secrets on argv (the token goes through an MCP config file at mode 600, deleted on exit).

## Testing strategy

- **e2e with a stub agent** (`MAINTENANCE_AGENT_COMMAND` override, like agent-runner's `AGENT_COMMAND`). The stub makes real MCP calls. Cover:
  - an ok run (report rewritten, log line, a broken link fixed, history and audit under `maintenance`);
  - a failing agent (exit 1) → `maintenance_failed` posted to the webhook stub;
  - a run that writes no report → failed;
  - an unreachable vault-mcp → failed;
  - `--dry-run` → no writes.
- **shellcheck** in CI.
- **Manual (live, Bookbot):** one real nightly run, with the report reviewed by a person, then one deliberately broken run (revoked token) to check the Slack alert.

## Boundaries

- **Always:** run after the backup; change the vault only through MCP; alert on failure.
- **Ask first:** raising the default budget; giving the agent shell or web tools.
- **Never:** run as root; edit `.env`, the policy or the ontology; delete pages.

## Success criteria

1. With cron at 00:00, the backup finishes first, then maintenance runs, and `_system/lint-report.md` has that night's date.
2. A source left in `raw/` during the day is ingested that night, and human docs are updated per doc-authoring.
3. A deliberately broken wikilink and a missing index entry are fixed by morning, with history entries under `maintenance`.
4. A failed run posts exactly one `maintenance_failed` alert to `#docs-vault-alerts`, with the reason.
5. A month of nightly runs stays within the budget setting, and the spend is printed in each log.

## Open questions

- None blocking.
