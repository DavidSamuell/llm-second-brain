# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Release notes flag breaking changes and vault template `schema_version` bumps.

## [Unreleased]

### Added

- `scripts/publish-vault-snapshot`: publish a one-way `vault/` documentation snapshot into an existing private GitHub plugin repository. It preserves every path outside `vault/`, uses an isolated Git cache/index, checks repository privacy and archive integrity, refuses secrets/binary containers/stale or foreign snapshots, and pushes without force.
- Backups now retain a stable `DATA_PATH/vault-id` UUID in their manifest for source binding. Existing archives remain restorable; publication requires a fresh identified archive. Hardlinked source files are archived as independent regular files.
- Setup guidance for combined plugin/document repositories, access implications, independent local snapshot creation and document-only recovery. Bookbot's midnight wrapper publishes to GitHub independently before its existing NAS backup and maintenance.
- Dreaming (SPEC-dreaming.md): `scripts/maintenance --dream`, a second nightly pass after the lint run. It checks the pages changed since the last dream against the sources they cite and against newer sources on the same subject, corrects or flags what doesn't match, merges duplicate agent-maintained wiki pages, keeps project `README.md` summaries short for `project_context`, records patterns that recur across recent sources, and writes `_system/dream-report.md`. The script picks the pages from history (`scripts/history changes`), leaving out the dream's own edits, and moves its window only past the pages the report lists as finished. It uses a new `maintenance-dream` token (run `scripts/maintenance --setup` again), defaults to a $7 budget, 60 minutes and 40 pages, costs nothing when nothing changed, and alerts `dream_failed` on failure. Session transcripts are not used.
- `scripts/history changes --since <time> [--exclude-credential <name>]`: JSON list of the paths changed at or after a time, oldest change first.

### Changed

- The nightly lint run no longer reads the whole vault every night. Structural checks still cover the whole vault, from tool output (`graph_lint`, folder listings, the index); semantic checks cover only the pages changed since the last lint (`--max-pages`, default 80, with the same `## Pages` coverage rule as the dream). A full semantic sweep runs on the first run, every `MAINTENANCE_FULL_EVERY_DAYS` days (default 7) and with `--full`. This keeps the nightly cost tied to how much changes, not to vault size.
- **Vault template schema 4 → 5:** `docs-capture` and AGENTS.md section 12 now treat ML findings as durable: dataset versions, splits and normalisation rules; final evaluation results with their experiment-tracker run link; eval protocol and baselines; negative results; training gotchas; error-analysis conclusions; model lineage, promotion criteria and export accuracy loss. A hypothesis under active test may be recorded only under a wiki page's `## Open questions`, marked unconfirmed. The session-start and prompt-nudge hook texts name ML findings too. Run `scripts/upgrade-vault`; released schema-4 files upgrade with custom blocks kept (new `schema4-kit` fixture).

### Fixed

- The nightly Ignis browser check now creates its own note through MCP instead of reusing the index recently saved by the history tests. This removes cross-test `recently_edited` failures without disabling the human-edit grace period or weakening the editor-refresh assertions.
- Corrected Ruff formatting in the isolated Ignis browser test so the repository-wide CI formatting gate passes; test behavior and checks are unchanged.
- Rewrote snapshot publisher guards as explicit conditions and removed an unused comparison result, preserving fail-closed checks while passing ShellCheck.

## [0.2.0] - 2026-11-17

The second brain (SPEC.md §18): agents keep the human docs and a typed knowledge graph current, team members' agents use and feed the vault while they work, and a nightly agent maintains it. Verified on the live Bookbot deployment. **Vault template schema 1 → 4**, and **breaking for vault layout** (schema 4, below): run `scripts/upgrade-vault` and update `write.allow` in `vault-mcp.yaml`.

### Added

- Opt-in agent editing of human-maintained pages with required change summaries, audit attribution and one review-queue notice per page per UTC day. The example policy now includes the human-doc folders (`domains/`, `shared/`); existing policies retain `read_only` unless changed.
- Protected rule paths (`write.protected`) and a 90-second grace after recorded web-editor saves (`human_edit_grace_s`), including move/link-update checks. Protected writes return `forbidden` with reason `protected`; recent saves return `recently_edited` with a retry delay. Stale-base writes still return `conflict`.
- Derived typed knowledge graph in `DATA_PATH/graph.sqlite`, incrementally maintained by vault-watcher and rebuilt by reindex/restore; admin ontology with last-valid reload and alerting.
- Read-scope MCP tools `graph_neighbors`, `graph_edges`, `graph_path`, `backlinks` and `graph_lint`, with endpoint/declaring-page permissions, evidence redaction, and ambiguity-safe references. MCP writes reject invalid typed metadata; browser edits remain editable and are reported by lint.
- Graph-aware moves preserve explicit ids and rewrite derived-id, source and project references without changing evidence quotations.
- Read-scope `project_context(repo?, project?)` with SSH/HTTPS repo normalization, permission-filtered project summary/status, one-hop graph, review items and five recent changes within 4,000 serialized characters.
- Immediate `document`, `ingest`, `ask` and `lint` plugin commands plus `docs-capture`; durable findings are fully ingested during the session, with source provenance, human docs and atomic wiki references.
- Vault schema 2: shared human-doc templates, project/repo/graph metadata, change logs and managed ontology; upgrades preserve released schema-1 hashes and custom blocks.
- Opt-in, credential-free session hooks for Claude Code and omp: repo allowlist, per-repo/session opt-outs, startup/compaction context and every-tenth-prompt capture reminders. Codex CLI automatic capture remains unsupported.
- Host `scripts/maintenance` for post-backup ingestion, structural/semantic lint and direct fixes through MCP, with estimated-spend/time limits, private logs, fresh-report checks, read-only preview and one Slack-compatible failure alert per run.
- Vault schema 3: ingest and in-session capture now require every human doc they create or change (decision records included) to be the object of a `documented_in` edge from a wiki page about the component or concept it concerns. Run `scripts/upgrade-vault`; released schema-1 and schema-2 files upgrade with custom blocks kept.

### Changed

- **Breaking, vault schema 4:** human docs are grouped by domain and project: `domains/<domain>/README.md`, `domains/<domain>/<project>/README.md` with `specs/`, `decisions/`, `pipelines/` and `runbooks/` beside it, `domains/<domain>/shared/` for docs several projects of a domain use, and top-level `shared/` for docs spanning domains. The flat `projects/`, `specs/`, `pipelines/` and `runbooks/` folders are gone. New `domain` template and graph node type. People create domains; agents ask first, and when unattended they hold back a new project's docs and file a review item. `project_context` finds projects only at `domains/<domain>/<project>/README.md` and also accepts the project folder name. Update `write.allow` in your `vault-mcp.yaml` (example: `[wiki/, raw/, _system/, domains/, shared/]`); `upgrade-vault` updates the rules and skills but does not move existing pages: move them with the `move` tool before upgrading agents.
- A human doc's folder follows its `project:` list (one project, several in one domain, several domains). Agents move a doc with `move` when its list changes; `docs-lint` finds and moves misplaced docs.

### Fixed

- The generated Claude Code plugin no longer pins `version`, so `claude plugin update` installs each rebuild the admin pushes (Claude Code versions it by commit). Update instructions now include the plugin update step for Claude Code and omp.
- Wikilinks inside an inline code span that wraps onto the next line no longer become graph `mentions` (a false `dangling_object` on the shipped `docs-ingest` skill).

## [0.1.0] - 2026-11-17

First release. Verified on a live Cloudflare deployment; the `caddy` profile is built and config-validated but not yet deployed on a public host (see `tasks/todo.md`, Deferred).

### Added

- `scripts/agent-plugin`: builds this deployment's `docs-vault` agent plugin as a marketplace that Claude Code, Codex and omp install from. The plugin carries the vault's MCP address (signed in through the browser with OAuth, no token to copy), the three kit skills, and a `connect` command. Publish the output as a git repository your team can read.
- `vault-core`: vault path rules, `vault-mcp.yaml` policy model with read/write decisions, advisory vault lock, atomic writes with sha256 versions, audit log, webhook alerts, and curated secret rules.
- `vault-mcp`: bearer-token credential store and `vault-mcp token create|list|revoke` (`scripts/token`); MCP server with `get_schema`, `read`, `list` over streamable HTTP (`/mcp`) and stdio, unauthenticated `/health`, audit line per tool call.
- `docker-compose.yml` with Ignis, vault-mcp and vault-watcher; ports bound to `BIND_ADDR`; `vault-mcp.example.yaml`.
- `vault-watcher`: version history for every change (inotify plus periodic scan, debounced web edits, git attribution, crash-safe hand-off of MCP writes), FTS5 search index, heartbeat and `/health`.
- `vault-mcp` `search` tool; `scripts/history` (`list`, `restore [--at]`, `restore-deleted`) and `scripts/reindex`.
- CI: lint (ruff, mypy, shellcheck, hadolint), unit, and end-to-end jobs.
- `docs/LIMITATIONS.md`: Ignis behaviour verified against the pinned image (shared layout, concurrent edits, on-disk changes while a note is open, mobile layout, read-only plugins mount, pinned Obsidian version, check-exposure markers).
- `vault-mcp` write tools: `write`, `append`, `replace_section`, `insert_after`, `review_queue_add`, and `move` (Obsidian-style link updates; refused if a page the agent can't edit links to the file). Every write gets a history version and an audit entry before it returns.
- `vault-mcp` source lifecycle: `submit_source`, `claim_source`, `complete_source`, `release_source`, with leases in `/data/state.db` and a sweeper that returns expired claims to `raw/`.
- Per-credential rate limits from `vault-mcp.yaml` (`rate_limited` error with `retry_after_s`); `rate_limit_repeated` alert.
- `vault-watcher` secret scan of every changed text file: high alert, masked `#review/high` review-queue item, audit entry; the secret is never repeated.
- `vault-watcher` ops monitor: alerts when vault-mcp or Ignis is unhealthy, a heartbeat is stale, or `/vault` or `/data` is over 80 % full. New settings: `MONITOR_INTERVAL_S`, `MONITOR_PEERS`, `MONITOR_HEARTBEATS`. `ALERT_WEBHOOK` now also reaches vault-mcp.
- `vault-mcp` OAuth 2.1 authorization server for hosted MCP clients (Claude.ai, ChatGPT): metadata, dynamic client registration, authorization code + PKCE (S256), rotating refresh tokens with reuse detection, revocation, sign-in with one-time approval codes (`vault-mcp oauth approve-code`), and a consent page. Grants appear in `token list` and end with `token revoke`. New settings: `MCP_OAUTH_ENABLED`, `MCP_OAUTH_ISSUER`.
- OAuth sign-in through Google, Microsoft Entra (single tenant), GitHub (primary verified email) or any OIDC provider, limited to `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`; upstream tokens are discarded. New settings: `OAUTH_UPSTREAM_PROVIDER`, `OAUTH_UPSTREAM_CLIENT_ID`, `OAUTH_UPSTREAM_CLIENT_SECRET`, `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`, `OAUTH_UPSTREAM_ISSUER`, `OAUTH_UPSTREAM_TENANT`.
- `vault-template/`: AGENTS.md (schema_version 1) with safety rules and the ingest/query/lint procedures, CLAUDE.md, Home.md, folder tree, 12 page templates, Obsidian settings, `.gitignore`, and `.kit-manifest.json` (managed-file hashes for upgrades).
- Kit skills `docs-ingest`, `docs-query`, `docs-lint` (Agent Skills format) in `vault-template/skills/`, for vault-mcp tools or a local git clone.
- `ignis-init` one-shot service: Obsidian community plugins limited to the checksum-pinned allowlist in `ignis/ignis-plugins.yaml` (empty by default) and mounted read-only into Ignis; kit-managed `.obsidian` settings reset at each start. New settings: `IGNIS_PLUGINS_FILE`, `IGNIS_PLUGIN_BASE_URL`.
- `scripts/setup` (interactive or `--non-interactive`): writes `.env` and `vault-mcp.yaml`, creates folders, copies or adopts the vault template, creates the agent-runner token, starts the stack and checks exposure. `.env.example` lists every setting.
- Ingress profiles: `cloudflare` (Cloudflare Tunnel; Access in front of the editor, Bypass for the MCP host) and `caddy` (Let's Encrypt HTTPS with an oauth2-proxy login in front of the editor), each with a guide in `docs/ingress/`.
- `scripts/check-exposure`: §5.4 checks (HTTPS, editor login, MCP 401 with OAuth discovery, bearer token, public OAuth metadata, raw ports) with PASS/FAIL/SKIP output; `scripts/setup` runs it.
- `git-sync` service (`GIT_REMOTE`): commits and pushes the vault every `SYNC_INTERVAL`, keeps conflicting local edits as `.conflict-<ts>.md` files and alerts, optional Git LFS; setup creates an SSH deploy key.
- `scripts/upgrade-vault`: shows and applies kit template updates (AGENTS.md, templates, skills, Obsidian settings), keeping `<!-- custom -->` blocks and anything in `.vault-overrides`.
- `agent-runner` service (`ANTHROPIC_API_KEY`, a Claude subscription token in `CLAUDE_CODE_OAUTH_TOKEN`, or `AGENT_COMMAND`): scheduled ingest and lint with Claude Code through vault-mcp only, a monthly budget (skip + alert), spend tracking, and git commit requests.
- `scripts/backup` (hot, SQLite-safe, checksummed, optional config) and `scripts/restore` (verified, into stopped and empty folders, then reindex).
- CI: nightly end-to-end run with Playwright browser checks against the pinned Ignis; release workflow publishing multi-arch images to GHCR on version tags; Dependabot.
- Documentation: SETUP, UPGRADING, USER-GUIDE, CONNECT-AGENTS, IMPORTING, SECURITY (threat model, offboarding) and final LIMITATIONS.

### Security

- OAuth hardening from the Checkpoint D review (`docs/reviews/2026-11-10-oauth-security-review.md`).
- Ignis server plugins (e.g. Headless Sync) are reset at each start unless `IGNIS_SERVER_PLUGINS` allows them, and an alert fires when one is enabled.
- Admin CLI actions are audited; `scripts/oauth revoke-codes` cancels a person's unused approval codes.
- Tool paths that go through a symlink are refused, even when the target is inside the vault, and the watcher doesn't record or index them, so an alias can't carry one folder's permissions to another.
