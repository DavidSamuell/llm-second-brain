# Docs Vault Kit — Task List

Source: `SPEC.md` v0.4. Decisions `D1…D39` are defined in `tasks/plan.md` § Architecture Decisions.

**Mode:**
- **watch** — the task depends on one or more D-decisions. Review the implementation against those decisions.
- **delegate** — the task depends only on what SPEC.md fixes, so it can be handed off without decision review.

Commands used in verification:
- `uv run pytest <pkg>`
- `uv run ruff check . && uv run mypy`
- `docker compose up -d --wait`
- `uv run pytest e2e -k <marker>`

---

## Phase 0 — Risk spikes (throwaway code; findings recorded, code deleted)

- [x] **T1 — Ignis behaviour spike** · Decisions: D4, D28, D29, D35 · **watch**
  - Run the pinned `nobbe/ignis` with the D4 mounts and record each §8 "verify during build" item:
    - whether workspace/layout state is shared between users
    - two users editing the same note
    - a file open in the editor that changes on disk (§16.12)
    - mobile browsers
    - Ignis' behaviour with a read-only `.obsidian/plugins` mount (D28)
    - the Obsidian version actually loaded when `OBSIDIAN_VERSION` = the pinned value (D29)
    - the HTML marker check-exposure uses to recognise Ignis (D35)
  - Acceptance: every item has a pass/fail plus observed behaviour in `docs/LIMITATIONS.md`. D28 fallback chosen if the RO mount breaks Ignis.
  - Verify: manual, two browsers + one mobile; screenshots linked in the doc.
  - Deps: none · Files: `docs/LIMITATIONS.md` · Scope: S

- [ ] **T2 — FastMCP OAuthProvider spike** · Decisions: D17 · **watch**
  - Minimal `OAuthProvider` subclass with endpoints under `/oauth/*` and metadata at the host root. Complete DCR + auth code + PKCE + refresh with the MCP Inspector, then with a real Claude.ai custom connector over a temporary tunnel.
  - Acceptance: the connector completes the flow and calls a dummy tool. Path customisation confirmed, or the D17 fallback written into plan.md.
  - Verify: manual connector run; notes in plan.md D17.
  - Deps: none · Files: plan.md only · Scope: S
  - **Status:** local part done (scripted MCP SDK client against fastmcp 3.4.11; the D17 fallback, SDK endpoints at the root, is written into plan.md). **Still open:** the real Claude.ai connector run, deferred by the user to Checkpoint D. Don't tick until it passes.

- [x] **T3 — History handoff spike** · Decisions: D6, D21, D22 · **watch**
  - Prototype: a watchdog observer in a container on a bind mount. A writer runs the D21 transaction: flock → prepare → `os.replace` → audit → commit rename → wait for ack. It makes 20 back-to-back writes to one file, interleaved with simulated Ignis-style rapid saves. Fault injection:
    - the watcher stopped before the write
    - the watcher killed between commit and ack
    - **vault-mcp killed** (a) after prepare and before replace, (b) after replace and before the audit append, (c) after the audit append and before the commit rename
    - an Ignis-style save between a vault-mcp kill and recovery
  - Acceptance:
    - 20/20 MCP versions recorded, including across watcher kill + restart.
    - vault-mcp kills: (a) → no version, audit `aborted`; (b) → version + exactly one `recovered` audit line; (c) → version, no duplicate audit line; save-before-recovery → `unconfirmed` pointer + alert, and the data is kept.
    - With the watcher stopped, writes are rejected and the vault file is unchanged.
    - p50/p95 ack latency measured.
    - `HISTORY_ACK_TIMEOUT` and the debounce defaults fixed in plan.md D21.
    - inotify delivery on the bind mount confirmed.
  - Verify: the spike script prints counts and latencies; results pasted into plan.md.
  - Deps: none · Files: plan.md only · Scope: S

## Phase 1 — Foundation + MCP read slice

- [x] **T4 — Project meta files** · Decisions: none · **delegate**
  - `LICENSE` (MIT, §17), `CHANGELOG.md` (Keep a Changelog, "Unreleased"), `README.md` with the §1 goal and a docs index.
  - Acceptance: files exist; LICENSE is the MIT text; README links every §14 doc path.
  - Verify: manual read.
  - Deps: none · Files: `LICENSE`, `CHANGELOG.md`, `README.md` · Scope: XS

- [x] **T5 — uv workspace scaffold + lint/unit CI** · Decisions: D1, D2, D38 · **watch**
  - Root `pyproject.toml` workspace with `vault-core/`, `vault-mcp/`, `vault-watcher/`; ruff/mypy/pytest config; `.github/workflows/ci.yml` jobs `lint` + `unit`.
  - Acceptance: `uv sync` works from a clean checkout; one placeholder test per package passes; CI green on a PR.
  - Verify: `uv run ruff check . && uv run mypy && uv run pytest`.
  - Deps: none · Files: `pyproject.toml`, `uv.lock`, `*/pyproject.toml`, `.github/workflows/ci.yml` · Scope: M
  - **Status:** local acceptance met under the user's "build and test locally, list external checks" allowance (set at the /build auto checkpoint). `uv sync --locked --all-packages` works from a fresh clone; ruff, ruff format, mypy and pytest (3 smoke tests) pass; actionlint finds no problems in `ci.yml`. The lint job also runs shellcheck on `scripts/**` + `*.sh` and hadolint on Dockerfiles (D38); both skip cleanly while no such files exist, and both steps were tested locally against a sample script and Dockerfile.
  - [ ] **External check (listed for the user):** CI green on a PR. Needs a push to GitHub, which isn't authorised in this run.

- [x] **T6 — vault-core: paths, policy, config** · Decisions: D8, D9, D27 · **watch**
  - Path normalisation and escape rejection; `vault-mcp.yaml` pydantic model; scope intersection; the maintainer write rule as pure functions.
  - Acceptance: `..`, absolute, symlink-escape, dotfile, and deny paths are rejected; tests cover the D9 truth table (allow/deny × maintainer × new file); an invalid YAML policy raises a clear error.
  - Verify: `uv run pytest vault-core -k "paths or policy"`.
  - Deps: T5 · Files: `vault-core/src/vault_core/{paths,policy,config}.py`, tests · Scope: M

- [x] **T7 — vault-core: lock, atomic write, version** · Decisions: D6, D7 · **watch**
  - flock context manager (sync + async) with an owner file; atomic same-directory replace that preserves mode; sha256 versioning.
  - Acceptance: two processes contending serialize; interop with `flock(1)` proven; a crash mid-write leaves the old file intact; no temp files left behind.
  - Verify: `uv run pytest vault-core -k "lock or atomic"`, multi-process tests included.
  - Deps: T5 · Files: `vault-core/src/vault_core/{lock,fsio}.py`, tests · Scope: S

- [x] **T8 — vault-core: audit, alerts, secret rules** · Decisions: D20, D24, D25 · **watch**
  - JSONL audit appender; webhook sender with Slack/Discord/generic shapes + dedupe; the curated secret rule set with a diff-only scan helper and masking.
  - Acceptance: each rule has positive and negative fixtures (including sha256 strings that must **not** match); alerts payload-tested against a local HTTP stub; concurrent audit appends never interleave.
  - Verify: `uv run pytest vault-core -k "audit or alerts or secrets"`.
  - Deps: T5 · Files: `vault-core/src/vault_core/{audit,alerts,secrets}.py`, tests · Scope: M

- [x] **T9 — vault-mcp credential store + token CLI** · Decisions: D16, D33 · **watch**
  - `tokens.db` schema; `vault-mcp token create|list|revoke`; bearer verification (hash lookup, expiry, revocation, `last_used_at`); `scripts/token` wrapper.
  - Acceptance: a token is shown once and only its hash stored; a revoked or expired token fails verification; `--folders`, `--scope`, `--expires` are persisted; `never` must be explicit.
  - Verify: `uv run pytest vault-mcp -k tokens`.
  - Deps: T6 · Files: `vault-mcp/src/vault_mcp/{store,cli}.py`, `scripts/token`, tests · Scope: M
  - **Status:** store and CLI tested (39 tests; mutation checks on revocation, hash-only storage, scope and expiry). `scripts/token` passes shellcheck; it runs the CLI in the `vault-mcp` service, which T10 adds, so its end-to-end run happens in T10/T11.

- [x] **T10 — vault-mcp read slice + compose base** · Decisions: D3, D4, D5, D10, D13, D16 · **watch**
  - FastMCP server: streamable HTTP `/mcp`, stdio mode (`VAULT_MCP_TOKEN`), `/health`. Tools `get_schema`, `read`, `list` with bearer auth and scope/deny enforcement. Dockerfile. `docker-compose.yml` with `ignis` + `vault-mcp`, binding `${BIND_ADDR}`, plus the `BIND_ADDR=0.0.0.0` startup warning.
  - Acceptance: no/invalid token → 401 with `WWW-Authenticate`; a read token can read and list in scope and gets `forbidden` outside it; binary files return base64.
  - Verify: `docker compose up -d --wait` then `uv run pytest vault-mcp`.
  - Deps: T6, T7, T9 · Files: `vault-mcp/src/vault_mcp/{server,tools_read}.py`, `vault-mcp/Dockerfile`, `docker-compose.yml` · Scope: M
  - **Status:** 64 vault-mcp tests (HTTP via uvicorn, stdio via subprocess; mutation checks on auth, scope and audit). Against the real compose stack: both services healthy; `/health` 200; `/mcp` without a token → 401 + `WWW-Authenticate: Bearer`; a token made with `scripts/token` reads `wiki/`, gets `forbidden` for `private/`, and every call is in `audit.log`; the BIND_ADDR warning shows for `0.0.0.0`; editing in Ignis works with the D4 `WS_ORIGINS` value. Follow-up fix: `get_schema` and `list` now go through the vault-escape guard too (an `AGENTS.md` or listed entry symlinked out of the vault is refused or skipped; regression tests added).

- [x] **T11 — e2e harness + CI e2e job** · Decisions: D5, D38 · **watch**
  - `e2e/` pytest suite: brings up the stack with `INGRESS=local` and `none`, creates tokens through `docker compose exec`, uses the `fastmcp.Client`. Implement §16.2 (probe from a container on a separate network) and the read half of §16.5.
  - Acceptance: CI `e2e` job runs both profiles on every PR and passes.
  - Verify: `uv run pytest e2e -m "profile_local or profile_none"`.
  - Deps: T10 · Files: `e2e/`, `.github/workflows/ci.yml` · Scope: M
  - **Status:** local acceptance met under the user's "build and test locally, list external checks" allowance. `uv run pytest e2e -m "profile_local or profile_none"`: 16 passed, 2 skipped. The skips are the other-network container probe: this host's firewall blocks container-to-host traffic, so its control listener is unreachable. §16.2 is still covered by the LAN-address probe, which was shown to fail when the stack is published on `0.0.0.0`. `ci.yml` has the `e2e` job (actionlint clean).
  - [ ] **External check (listed for the user):** the CI `e2e` job green on a PR (needs a push), including the other-network probe running unskipped on the GitHub runner.

### Checkpoint A — after T4–T11
- [ ] lint, unit, and e2e green in CI (locally: ruff, mypy, shellcheck, hadolint, 198 unit tests and the e2e suite pass; CI needs a push)
- [x] §16.2 passes; a bearer read works end-to-end against the compose stack
- [ ] Human review of the Open Questions in plan.md before Phase 2

## Phase 2 — Watcher: history, index, search

- [x] **T12 — vault-core: history store** · Decisions: D21 · **watch**
  - Blob + pointer layout, `record`, `head`, `list`, `restore`, `restore_deleted`, retention GC (age + min versions + blob sweep). D21 transaction format: a prepare/commit writer (used by vault-mcp), plus an idempotent consumer and a prepared-entry classifier (`applied` / `aborted` / `unconfirmed`) used by the watcher, keyed by `audit_id` + kind.
  - Acceptance: identical content is stored once; GC keeps ≥ `HISTORY_MIN_VERSIONS` per file regardless of age and never touches the spool; tombstones allow restoring deleted files; consuming the same committed entry twice yields one pointer; the classifier gives the D21 table's result for each file state (post hash / pre hash or absent / other); timestamps are UTC.
  - Verify: `uv run pytest vault-core -k history`.
  - Deps: T7 · Files: `vault-core/src/vault_core/history.py`, tests · Scope: M

- [x] **T13 — vault-watcher service: detection + history** · Decisions: D3, D6, D21, D22 · **watch**
  - watchdog observer + full scan; spool draining under the lock (startup, `.inbox` events, and before every vault event); prepared-entry recovery with audit repair (`recovered` / `aborted` / `unknown`) and alert; ack by deleting the entry after the pointer is fsynced; heartbeat file; debounce for non-MCP writers; attribution from the lock owner; startup baseline; daily GC; `/health`; Dockerfile; compose service.
  - Acceptance: a shell edit creates a `web` version after the debounce; a committed write is recorded as `mcp` with `audit_id`, and its fs event is skipped as a duplicate; a delete creates a tombstone; events missed while stopped are caught by the scan; committed and prepared entries left by crashes are resolved at startup per the D21 recovery table; at startup, any file whose hash differs from its newest pointer is recorded (`source: unknown`), including content saved after a vault-mcp crash and before recovery (gap seen in T3).
  - Verify: `uv run pytest vault-watcher -k history` (temp dirs) + `docker compose up -d --wait`.
  - Deps: T3, T8, T12 · Files: `vault-watcher/src/vault_watcher/{main,events,recorder}.py`, `vault-watcher/Dockerfile`, `docker-compose.yml` · Scope: L (split by module if it grows)
  - **Status:** 12 watcher tests on real directories with a real watchdog observer (5 repeated runs stable; mutation checks on the startup scan, git attribution, audit repair, head dedupe, temp cleanup and the ignore list). In the compose stack (e2e, both profiles): the watcher is healthy, the startup scan records baselines, a host-side edit becomes a `web` version, and the heartbeat is fresh.

- [x] **T14 — Search index + MCP `search` tool** · Decisions: D8, D10, D23 · **watch**
  - FTS5 schema, incremental updates, rebuild at startup; `search(query, folder?, limit?)` in vault-mcp opening the database read-only with scope filtering.
  - Acceptance: `read.deny` files never appear in the database; folder scope filters results; FTS syntax characters in a query cause no error; ranking prefers title matches.
  - Verify: `uv run pytest vault-watcher -k index && uv run pytest vault-mcp -k search` + an e2e §16.7 search case.
  - Deps: T10, T13 · Files: `vault-watcher/src/vault_watcher/index.py`, `vault-mcp/src/vault_mcp/tools_search.py`, tests · Scope: M
  - **Status:** index schema, writer and query are in `vault-core/src/vault_core/search_index.py` (shared, as D2 intends), and the watcher keeps the index current (startup rebuild, snapshots, MCP writes, deletes). Tests cover: deny, hidden and non-Markdown files never indexed; FTS syntax characters never error and `OR` is a plain word; title matches rank first; accents folded; scope applied in SQL before the limit; folder argument errors. e2e §16.7 search case passes in both profiles.

- [x] **T15 — history / reindex CLIs + scripts** · Decisions: D21, D23, D33 · **watch**
  - `vault-watcher history list|restore|restore-deleted`, `vault-watcher reindex`; bash wrappers `scripts/history`, `scripts/reindex`. A restore writes through the lock and is recorded as a new version.
  - Acceptance: §16.14 passes, simulating Ignis edit and delete with filesystem writes; `restore --at` picks the right version.
  - Verify: `uv run pytest e2e -k "history_restore"`.
  - Deps: T13, T14 · Files: `vault-watcher/src/vault_watcher/cli.py`, `scripts/history`, `scripts/reindex` · Scope: S
  - **Status:** 11 CLI tests (list; restore latest and `--at` by ISO time or listed timestamp; restore-deleted per folder; error cases; reindex). A restore writes under the lock (owner `restore`), is recorded as a new version (`kind: restore`), and is re-indexed. e2e §16.14 passes in both profiles. Wrappers pass shellcheck.

### Checkpoint B — after T12–T15
- [x] §16.14 passes (filesystem-simulated web edits)
- [x] The search e2e respects deny and scope rules

## Phase 3 — MCP write path, secret scan, ops monitor

- [x] **T16 — `write` + `append`** · Decisions: D7, D8, D9, D10, D20, D21, D24 · **watch**
  - Version-checked create/replace, append under the lock; D21 handoff (liveness pre-flight, pre/post-image spool, ack wait); diff-only secret rejection; `max_file_mb`; audit line before releasing the lock.
  - Acceptance:
    - §16.9: a stale `base_version` → `conflict`, nothing lost.
    - §16.10: a write to a human page → `forbidden`.
    - The MCP half of §16.13.
    - The write half of §16.5: a read token cannot write.
    - §16.15: every successful write has an audit line and a history version. This includes 20 back-to-back writes; an Ignis-style save right after an MCP write (both versions kept); the watcher killed mid-ack (`history_pending` with `applied: true`, and the version appears after restart); and vault-mcp killed at each side of `os.replace` and of the audit append (no phantom versions, no versions missing an audit line, no duplicate audit lines).
    - Watcher stopped → `history_pending` and the file is unchanged.
  - Verify: `uv run pytest vault-mcp -k write` + `uv run pytest e2e -k "conflict or human_page or audit_history or secret_mcp"`.
  - Deps: T13 · Files: `vault-mcp/src/vault_mcp/tools_write.py`, tests · Scope: M
  - **Status:** the pipeline is `vault-mcp/src/vault_mcp/writer.py`; tests run against a real vault-watcher. vault-mcp is killed in subprocesses before the rename, before the audit line, and before the commit; no phantom versions, missing audit lines or duplicates. Mutation checks cover the base-version, permission, read-only, pre-image, acknowledgement, secret and audit-ordering branches. e2e (both profiles): read token can't write, §16.9 conflict, §16.10 refusal, §16.13 MCP rejection, §16.15 five writes ↔ five audit ids ↔ five history versions.

- [x] **T17 — `replace_section` + `insert_after`** · Decisions: D10, D11 · **watch**
  - markdown-it-py section locator; both tools reuse the T16 write pipeline.
  - Acceptance: headings inside code fences are ignored; duplicate headings → `ambiguous_heading` with candidates; setext headings work; content outside the section is byte-identical afterwards.
  - Verify: `uv run pytest vault-mcp -k sections`.
  - Deps: T16 · Files: `vault-mcp/src/vault_mcp/{sections,tools_write}.py`, tests · Scope: S
  - **Status:** 11 section tests (fences ignored, setext, frontmatter not a heading, subsections inside a section, last section to EOF, CRLF outside kept, level pinned by a full heading line, ambiguity with candidates) plus a pipeline test through the writer. Mutation checks on section end, frontmatter stripping, ambiguity and level matching.

- [x] **T18 — `review_queue_add`** · Decisions: D15 · **watch**
  - Appends a D15-format item to `_system/review-queue.md`, creating the file if missing. Requires write scope and is exempt from the maintainer rule, so it works for `maintainer: human` pages.
  - Acceptance: §16.10 second half; items render as Obsidian tasks with a block id.
  - Verify: `uv run pytest vault-mcp -k review_queue` + e2e §16.10.
  - Deps: T16 · Files: `vault-core/src/vault_core/review_queue.py`, `vault-mcp/src/vault_mcp/tools_write.py` · Scope: S
  - **Status:** format tests (single Obsidian task line, `#review` vs `#review/high`, newline and `[[` injection neutralised, creation with a header); writer tests (a folder-scoped credential can still add to the queue; denied pages and read tokens are refused; one history version per item); e2e §16.10 second half in both profiles.

- [x] **T19 — `move` + link rewriting** · Decisions: D8, D9, D12, D21 · **watch**
  - Obsidian-style link resolution; backlink preflight; all-or-nothing rename + link rewrites as one D21 transaction group; history and audit per touched file.
  - Acceptance:
    - Wikilink, alias, heading, embed, and relative markdown links are all updated.
    - A backlink in a `maintainer: human` page, an out-of-scope file, or a `read.deny` file → `forbidden` with no file changed. Visible blockers are listed by path; hidden ones only as a count.
    - A stale `base_version` → `conflict`.
    - vault-mcp killed mid-group → the watcher alerts with the partly applied group.
  - Verify: `uv run pytest vault-mcp -k move`.
  - Deps: T16 · Files: `vault-mcp/src/vault_mcp/{links,tools_move}.py`, tests · Scope: M
  - **Status:** 14 link tests (resolution by path suffix, ties to the linking folder, aliases, headings, embeds, relative Markdown links, code ignored, ambiguous new names fall back to full paths, relative links inside the moved page rebased) and 14 move tests: every link style updated; one history version and audit line per touched file with a shared `group_id`; human-page, read-denied, out-of-scope and hidden-page linkers each block the whole move (hidden ones as a count only); trash ignored; refusals change nothing; vault-mcp killed mid-group → `move_partial` alert; a browser save between two group replacements isn't overwritten. Mutation checks on blockers, the hidden-page scan, the per-file re-check and old-path removal.

- [x] **T20 — Source lifecycle tools + lease sweeper** · Decisions: D10, D14, D21 · **watch**
  - `submit_source`, `claim_source`, `complete_source`, `release_source`; `/data/state.db` leases; 60 s sweeper.
  - Acceptance: §16.11 (two concurrent claims → exactly one wins; an expired lease returns the file to `raw/`); a URL-only submit creates a stub with no network call; only the lease holder can complete or release.
  - Verify: `uv run pytest vault-mcp -k sources` + e2e §16.11 (lease TTL set to seconds).
  - Deps: T16 · Files: `vault-mcp/src/vault_mcp/{sources,leases}.py`, tests · Scope: M
  - **Status:** 17 source tests (dated slug names and collision suffixes, YAML-safe provenance frontmatter, URL stubs with no network access, URL scheme checks, concurrent claims with exactly one winner, holder-only complete and release, `summary_page` must exist and be readable, only files directly in `raw/`, expiry sweep, sweep of leaseless orphans in `_processing`). e2e §16.11 in both profiles: one of two concurrent claims wins; a 1-minute lease is returned to `raw/` by the running sweeper.

- [ ] **T21 — Rate limiting + repeated-hit alert** · Decisions: D19, D25 · **watch**
  - Sliding-window middleware for rpm, writes/min, writes/day; `rate_limited` with `retry_after_s`; alert threshold.
  - Acceptance: §16.8 (limit exceeded → error; ≥ 5 hits in 10 min → exactly one webhook call within the hour).
  - Verify: `uv run pytest vault-mcp -k ratelimit` + e2e §16.8 with a webhook stub container.
  - Deps: T10, T8 · Files: `vault-mcp/src/vault_mcp/ratelimit.py`, tests · Scope: S

- [ ] **T22 — Watcher secret scan** · Decisions: D15, D20, D24, D25 · **watch**
  - Scan changed files from any writer; dedupe findings; masked `#review/high` item; audit line; alert.
  - Acceptance: the Ignis half of §16.13 (a fake key written via the filesystem triggers one alert + one review item, and the secret is not repeated anywhere); the review-queue write does not re-trigger the scan; human content is never edited.
  - Verify: `uv run pytest vault-watcher -k secrets` + e2e §16.13.
  - Deps: T13, T18 · Files: `vault-watcher/src/vault_watcher/secret_scan.py`, tests · Scope: S

- [ ] **T23 — Ops monitor + healthchecks** · Decisions: D25, D26 · **watch**
  - Compose healthchecks for all services; a watcher monitor loop (peer `/health`, heartbeat age, disk > 80 %).
  - Acceptance: stopping vault-mcp → alert after 3 failed checks; a fake disk-usage provider at 81 % → alert; no Docker socket mounted anywhere.
  - Verify: `uv run pytest vault-watcher -k monitor` + an e2e case stopping vault-mcp.
  - Deps: T13 · Files: `vault-watcher/src/vault_watcher/monitor.py`, `docker-compose.yml` · Scope: S

### Checkpoint C — after T16–T23
- [ ] §16.5, 7, 8, 9, 10, 11, 13, 14, 15 green in CI e2e
- [ ] Manual: the Claude Code CLI connected with a bearer token can read, write, and claim

## Phase 4 — OAuth

- [ ] **T24 — OAuth authorization server + approval codes** · Decisions: D16, D17 · **watch**
  - `OAuthProvider` subclass on `tokens.db`; DCR, authorize, token, refresh rotation with reuse detection, revoke; metadata; consent page; `vault-mcp oauth approve-code`; grants listed and revoked through `token list|revoke`; `MCP_OAUTH_ENABLED=false` disables every OAuth route.
  - Acceptance: a protocol test drives the full flow with PKCE (S256 only); a revoked grant's access and refresh tokens are rejected right away; an approval code works once.
  - Verify: `uv run pytest vault-mcp -k oauth`.
  - Deps: T2, T9, T10 · Files: `vault-mcp/src/vault_mcp/oauth/{provider,pages,codes}.py`, tests · Scope: L (split pages from provider if needed)

- [ ] **T25 — Upstream OIDC login** · Decisions: D18 · **watch**
  - authlib flows for google, microsoft, generic oidc, and github (verified-email API); email domain restriction; upstream tokens discarded.
  - Acceptance: a disallowed domain is denied at login; an unverified GitHub email is rejected; owner = verified email.
  - Verify: `uv run pytest vault-mcp -k upstream` against a local mock OIDC server (e.g. `mock-oauth2-server` container) + manual run with a real Google app.
  - Deps: T24 · Files: `vault-mcp/src/vault_mcp/oauth/upstream.py`, tests · Scope: M

### Checkpoint D — after T24–T25
- [ ] §16.6 manual: the Claude.ai connector completes both modes, scopes are enforced, and revocation works
- [ ] Security review of the OAuth code (`security-auditor`) before continuing

## Phase 5 — Template, skills, Ignis hardening, setup

- [ ] **T26 — vault-template content** · Decisions: D9, D14, D15, D36 · **watch**
  - `AGENTS.md` (integer `schema_version`, maintainer rules, ingest/query/lint procedures, prompt-injection defence), `CLAUDE.md`, `Home.md`, folder tree, 12 page templates, `.obsidian/app.json`, `.gitignore`, `_system/*`, `.kit-manifest.json`.
  - Acceptance: every §9 bullet is present; procedures use the exact D10/D14/D15 tool contracts; the manifest hashes match the files.
  - Verify: a manifest check script in unit tests; manual read of AGENTS.md.
  - Deps: T20 · Files: `vault-template/**` · Scope: L (content only)

- [ ] **T27 — Kit skills (`docs-ingest`, `docs-query`, `docs-lint`)** · Decisions: D10, D11, D14, D15 · **watch**
  - Agent Skills format; MCP-tool mode, plus filesystem mode for agents that read the vault directly.
  - Acceptance: each skill follows AGENTS.md; ingest uses `claim_source` → edits → `complete_source` and `release_source` on failure; lint archives ticked review items.
  - Verify: manual run of each skill through Claude Code against the local stack.
  - Deps: T26 · Files: `vault-template/skills/**` · Scope: M

- [ ] **T28 — Ignis hardening init** · Decisions: D4, D28, D29 · **watch**
  - `ignis/ignis-plugins.yaml`, `ignis/harden.py`, the `ignis-init` one-shot service, the read-only plugin mount, managed-settings reset, pinned `IGNIS_IMAGE` + `OBSIDIAN_VERSION`.
  - Acceptance: §16.17 (a plugin dropped into the plugins dir is gone after restart and allowlisted plugins remain); a checksum mismatch aborts startup; §16.18 confirmed from the log.
  - Verify: `uv run pytest e2e -k "plugins or obsidian_version"`.
  - Deps: T1, T10 · Files: `ignis/**`, `docker-compose.yml` · Scope: M

- [ ] **T29 — `scripts/setup` + config examples** · Decisions: D4, D5, D16, D27, D34 · **watch**
  - `.env.example` (§13 + new vars), `vault-mcp.example.yaml`; interactive and `--non-interactive` setup; vault init/adopt; `COMPOSE_FILE` from `INGRESS` and `COMPOSE_PROFILES` for optional services; runner token; check-exposure hook (skipped until T32 exists).
  - Acceptance: a fresh `INGRESS=local` deploy on a clean VM finishes in < 30 min (§16.1 local); rerunning setup is idempotent.
  - Verify: CI runs `scripts/setup --non-interactive` in the e2e job; manual timed run on a clean VM.
  - Deps: T26, T28 · Files: `scripts/setup`, `.env.example`, `vault-mcp.example.yaml` · Scope: M

### Checkpoint E — after T26–T29
- [ ] §16.1 (local), 12, 17, 18 pass; nightly-style Ignis checks run manually

## Phase 6 — Ingress

- [ ] **T30 — `cloudflare` profile + guide** · Decisions: D5, D39 · **watch**
  - `ingress/cloudflare/compose.yml` override (cloudflared) selected through `COMPOSE_FILE`, and `docs/ingress/cloudflare.md` (tunnel, Access app, MCP Bypass app, 50-user / 100 MB limits).
  - Acceptance: §16.1 and §16.3 for cloudflare on a real domain.
  - Verify: manual deploy; check-exposure once T32 lands.
  - Deps: T29 · Files: `ingress/cloudflare/compose.yml`, `docs/ingress/cloudflare.md` · Scope: S

- [ ] **T31 — `caddy` profile + guide** · Decisions: D5, D39 · **watch**
  - `ingress/caddy/compose.yml` override (caddy + oauth2-proxy) selected through `COMPOSE_FILE`, plus `ingress/caddy/Caddyfile` and `docs/ingress/caddy.md`.
  - Acceptance: §16.1 and §16.3 for caddy; WebSockets work through forward_auth; `/mcp` and `/oauth/*` reachable without a browser login.
  - Verify: manual deploy on a public VM.
  - Deps: T29 · Files: `ingress/caddy/{compose.yml,Caddyfile}`, `docs/ingress/caddy.md` · Scope: M

- [ ] **T32 — `scripts/check-exposure`** · Decisions: D35 · **watch**
  - All §5.4 checks; PASS/FAIL/SKIP output; wired into setup.
  - Acceptance: §16.4 (passes for each profile; fails clearly against a stub proxy without auth, a fixture in e2e).
  - Verify: `uv run pytest e2e -k check_exposure`.
  - Deps: T29 · Files: `scripts/check-exposure`, `e2e/` · Scope: S

- [ ] **T33 — Ingress recipe docs** · Decisions: none · **delegate**
  - `docs/ingress/{pangolin,nginx,traefik,tailscale,headscale,local}.md`, each meeting the §5.1 contract and pointing at `127.0.0.1:IGNIS_PORT` / `MCP_PORT`; note the Tailscale free plan is non-commercial.
  - Acceptance: every recipe covers all five §5.1 requirements explicitly.
  - Verify: manual review against §5.1.
  - Deps: none · Files: `docs/ingress/*.md` · Scope: M

### Checkpoint F — after T30–T33
- [ ] §16.1 (cloudflare, caddy), §16.3, §16.4 verified on real hosts

## Phase 7 — Optional services and lifecycle

- [ ] **T34 — git-sync** · Decisions: D6, D25, D31, D32 · **watch**
  - Bash loop service (profile); lock/commit/rebase/push; conflict recovery; LFS; request-file commits; heartbeat; alerts.
  - Acceptance: §16.21 (edit reaches the remote within `SYNC_INTERVAL` + 1 min; an induced conflict → `.conflict-<ts>.md` + alert; `/data` never in the repo).
  - Verify: `uv run pytest e2e -k git_sync` with a bare local remote.
  - Deps: T23, T29 · Files: `git-sync/{Dockerfile,sync.sh}`, `scripts/lib/alert.sh`, `docker-compose.yml` · Scope: M

- [ ] **T35 — agent-runner** · Decisions: D16, D30, D31 · **watch**
  - Image, supercronic schedules, the `.mcp.json` generator, job lock, budget ledger, `AGENT_COMMAND` contract, commit request files.
  - Acceptance: §16.19 (submitted source is claimed, processed, completed, and logged); §16.20 (budget reached → skip + alert); a custom command without cost output → startup warning.
  - Verify: e2e with a stub `AGENT_COMMAND` emitting `total_cost_usd`; one manual run with real Claude Code.
  - Deps: T20, T27, T34 · Files: `agent-runner/**`, `docker-compose.yml` · Scope: M

- [ ] **T36 — `scripts/upgrade-vault`** · Decisions: D6, D21, D36 · **watch**
  - Schema compare; diff of AGENTS.md, templates, skills, and managed `.obsidian` files; manifest-based customisation detection; custom-block re-anchoring; `.vault-overrides`; apply on confirmation.
  - Acceptance: §16.22 (a newer template applies without touching `<!-- custom -->…<!-- /custom -->` blocks or override paths).
  - Verify: `uv run pytest vault-watcher -k upgrade` + e2e §16.22.
  - Deps: T26 · Files: `vault-watcher/src/vault_watcher/upgrade.py`, `scripts/upgrade-vault` · Scope: M

- [ ] **T37 — `scripts/backup` + `scripts/restore`** · Decisions: D4, D6, D37 · **watch**
  - Hot backup with SQLite online backup and a manifest; `--include-config`; restore with a stopped stack, followed by reindex.
  - Acceptance: §16.16 (backup → wipe → restore reproduces vault, history, tokens, Ignis state).
  - Verify: `uv run pytest e2e -k backup_restore`.
  - Deps: T13, T28 · Files: `scripts/backup`, `scripts/restore` · Scope: S

### Checkpoint G — after T34–T37
- [ ] §16.16, 19, 20, 21, 22 green

## Phase 8 — Release and docs

- [ ] **T38 — Nightly Ignis e2e, release pipeline, Dependabot** · Decisions: D38 · **watch**
  - Playwright nightly for §16.12, 17, 18; `release.yml` (tag → multi-arch GHCR images); `dependabot.yml`; `SECURITY.md` disclosure contact.
  - Acceptance: the nightly runs green against pinned Ignis; a test tag publishes amd64 and arm64 images.
  - Verify: workflow runs on a fork.
  - Deps: T28, T37 · Files: `.github/workflows/{nightly,release}.yml`, `.github/dependabot.yml`, `e2e/ignis/` · Scope: M

- [ ] **T39 — SETUP.md + UPGRADING.md** · Decisions: D5, D29, D34 · **watch**
  - Server requirements (CPU/RAM/disk, amd64/arm64), setup walkthrough per profile, upgrade procedure with the Ignis/Obsidian pin pair and a smoke test.
  - Acceptance: a person new to the kit completes §16.1 using only SETUP.md.
  - Verify: manual dry run by someone other than the author.
  - Deps: T29–T32 · Files: `docs/SETUP.md`, `docs/UPGRADING.md` · Scope: S

- [ ] **T40 — USER-GUIDE.md, CONNECT-AGENTS.md, IMPORTING.md** · Decisions: D14, D15, D16, D17 · **watch**
  - The non-technical editing and review-queue guide; per-client agent setup (Claude Code, Cursor, Codex, Claude.ai, ChatGPT); the importing flow, where agents fetch content themselves (D14).
  - Acceptance: each client snippet is tested against the local stack.
  - Verify: manual connection per client.
  - Deps: T24, T27 · Files: `docs/{USER-GUIDE,CONNECT-AGENTS,IMPORTING}.md` · Scope: M

- [ ] **T41 — SECURITY.md + LIMITATIONS.md (final)** · Decisions: D6, D16, D17, D20, D24, D28 · **watch**
  - Threat model, Ignis having no auth, plugin allowlist, token/OAuth lifecycle, secrets, offboarding checklist; LIMITATIONS covering T1 results, the Ignis/MCP write race, web attribution, Ignis legal basis, Obsidian terms.
  - Acceptance: every §14 bullet for both docs is covered; the offboarding checklist revokes bearer tokens + OAuth grants + IdP access.
  - Verify: `security-auditor` review of the docs against the code.
  - Deps: T1, T24, T28 · Files: `docs/SECURITY.md`, `docs/LIMITATIONS.md` · Scope: S

### Checkpoint H — complete
- [ ] All 22 §16 acceptance tests pass (CI e2e + nightly + documented manual runs)
- [ ] CHANGELOG updated; v0.1.0 tag published
