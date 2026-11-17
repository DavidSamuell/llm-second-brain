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
  - **Status:** local part done (scripted MCP SDK client against fastmcp 3.4.11; the D17 fallback, SDK endpoints at the root, is written into plan.md). The live-client run is now the Claude Code plugin (the user dropped the Claude.ai connector test on 2026-11-11; see §16.6 below).

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
  - [x] **External check:** CI green on a PR: [PR #1](https://github.com/DavidSamuell/llm-second-brain/pull/1), run 42746635890.

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
  - [x] **External check:** the CI `e2e` job green on PR #1 (run 42746635890): 50 passed, 0 skipped, so the other-network probe ran on the GitHub runner.

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

- [x] **T21 — Rate limiting + repeated-hit alert** · Decisions: D19, D25 · **watch**
  - Sliding-window middleware for rpm, writes/min, writes/day; `rate_limited` with `retry_after_s`; alert threshold.
  - Acceptance: §16.8 (limit exceeded → error; ≥ 5 hits in 10 min → exactly one webhook call within the hour).
  - Verify: `uv run pytest vault-mcp -k ratelimit` + e2e §16.8 with a webhook stub container.
  - Deps: T10, T8 · Files: `vault-mcp/src/vault_mcp/ratelimit.py`, tests · Scope: S
  - **Status:** 7 unit tests (requests/minute per credential, writes count toward both limits, writes/day with `retry_after_s` from the oldest counted write, refused calls not counted, alert after 5 refusals in 10 minutes at most hourly, refusals spread over more than 10 minutes don't alert) plus an in-process MCP test (`rate_limited` error + audit line). e2e §16.8 in both profiles: 130 rapid calls get `rate_limited`, and exactly one `rate_limit_repeated` alert reaches a test webhook stub (`e2e/compose.e2e.yml`). Mutation checks on counting refusals, the daily limit and alert throttling.

- [x] **T22 — Watcher secret scan** · Decisions: D15, D20, D24, D25 · **watch**
  - Scan changed files from any writer; dedupe findings; masked `#review/high` item; audit line; alert.
  - Acceptance: the Ignis half of §16.13 (a fake key written via the filesystem triggers one alert + one review item, and the secret is not repeated anywhere); the review-queue write does not re-trigger the scan; human content is never edited.
  - Verify: `uv run pytest vault-watcher -k secrets` + e2e §16.13.
  - Deps: T13, T18 · Files: `vault-watcher/src/vault_watcher/secret_scan.py`, tests · Scope: S
  - **Status:** 5 watcher tests (a browser save gives one high alert, one masked `#review/high` item and one audit line, the key appears in no output, and the page is untouched; no repeat for an unchanged finding; the queue write is recorded in history as `system` and doesn't re-trigger the scan; a new path is a new finding; dedupe survives a restart; clean pages and binaries stay quiet). e2e §16.13 filesystem half in both profiles, through the webhook stub. MCP writes are scanned by the watcher too (after commit/recovery), which matters when `reject_on_write` is off.

- [x] **T23 — Ops monitor + healthchecks** · Decisions: D25, D26 · **watch**
  - Compose healthchecks for all services; a watcher monitor loop (peer `/health`, heartbeat age, disk > 80 %).
  - Acceptance: stopping vault-mcp → alert after 3 failed checks; a fake disk-usage provider at 81 % → alert; no Docker socket mounted anywhere.
  - Verify: `uv run pytest vault-watcher -k monitor` + an e2e case stopping vault-mcp.
  - Deps: T13 · Files: `vault-watcher/src/vault_watcher/monitor.py`, `docker-compose.yml` · Scope: S
  - **Status:** 6 monitor tests (alert on the 3rd consecutive failure, once per outage, a recovery notice, a distinct subject per outage so the sender's hourly dedupe can't hide a second outage, one success resets the count, a crashing probe counts as a failure, disk 80 % ok / 81 % alert, heartbeat checked only for enabled services, HTTP probe). e2e in both profiles: stopping vault-mcp gives a `service_unhealthy` alert, restarting it gives `service_recovered`; the resolved compose config and every ingress override contain no `docker.sock`. Compose healthchecks already exist for ignis, vault-mcp and vault-watcher; git-sync and agent-runner get theirs in Phase 4 (the monitor already checks their heartbeats once their state directories exist).

### Checkpoint C — after T16–T23
- [x] §16.5, 7, 8, 9, 10, 11, 13, 14, 15 green in CI e2e — PR #1, run 42746635890: 50 passed, 0 skipped (the two firewall-skipped LAN tests run on GitHub's runners)
- [x] Manual: the Claude Code CLI connected with a bearer token can read, write, and claim — **passed 2026-11-10** with `/tmp/cli-check.py` on the david@bookbotkids.com Team account ($0.43). `get_schema`, `read`, `append`, `submit_source` and `claim_source` all returned ok, each with an `ok` audit line; the append is in history as `mcp`; the source is in `raw/_processing/`. The first attempt exposed a real bug, now fixed in de7b983: VS Code's file watchers had used up this host's per-user inotify watches, the watcher fell back to a 10-minute scan, and every write timed out with `history_pending`. Steps:
  1. `docker compose up -d`, then `scripts/token create --name claude-cli --owner you@example.com --scope write`
  2. `claude mcp add --transport http vault http://127.0.0.1:8082/mcp --header "Authorization: Bearer <token>"`
  3. In `claude`: "Call get_schema, read wiki/index.md, append a line to a wiki/ page, submit_source a short note, then claim_source it."
  4. Pass: every call succeeds, `audit.log` has an `ok` line per call, and `scripts/history list <page>` shows the append as `mcp`.

## Phase 4 — OAuth

- [x] **T24 — OAuth authorization server + approval codes** · Decisions: D16, D17 · **watch**
  - `OAuthProvider` subclass on `tokens.db`; DCR, authorize, token, refresh rotation with reuse detection, revoke; metadata; consent page; `vault-mcp oauth approve-code`; grants listed and revoked through `token list|revoke`; `MCP_OAUTH_ENABLED=false` disables every OAuth route.
  - Acceptance: a protocol test drives the full flow with PKCE (S256 only); a revoked grant's access and refresh tokens are rejected right away; an approval code works once.
  - Verify: `uv run pytest vault-mcp -k oauth`.
  - Deps: T2, T9, T10 · Files: `vault-mcp/src/vault_mcp/oauth/{provider,pages,codes}.py`, tests · Scope: L (split pages from provider if needed)
  - **Status:** 26 tests against a real uvicorn server. Covered: metadata advertises S256, `none` auth and root endpoints, and a 401 carries `resource_metadata`; unsafe redirect URIs are refused at registration; the full flow (DCR → authorize → approval code → consent → code + PKCE → token → tool call, with the grant in `token list` and the audit log); consent can narrow to read, and a read approval code caps the grant; deny returns `access_denied`; a wrong PKCE verifier fails; auth codes and approval codes are single use; 5 wrong codes kill the request and 30 failures lock sign-in for 10 minutes; another browser can't complete someone else's request; pages can't be framed; refresh rotates, and reusing an old refresh token revokes the whole grant; `token revoke` and RFC 7009 `/revoke` cut access immediately; bearer tokens keep working; the MCP SDK `OAuthClientProvider` completes the flow headlessly; `MCP_OAUTH_ENABLED=false` removes every OAuth route; a v1 `tokens.db` migrates; `oauth approve-code` CLI; OAuth use updates `last_used`. Mutation checks on reuse revocation, browser binding, approval-code reuse, scope cap, redirect rules, code reuse and the attempt cap. The `codes.py` split wasn't needed: approval codes live in `oauth/grants.py`.

- [x] **T25 — Upstream OIDC login** · Decisions: D18 · **watch**
  - authlib flows for google, microsoft, generic oidc, and github (verified-email API); email domain restriction; upstream tokens discarded.
  - Acceptance: a disallowed domain is denied at login; an unverified GitHub email is rejected; owner = verified email.
  - Verify: `uv run pytest vault-mcp -k upstream` against a local mock OIDC server (e.g. `mock-oauth2-server` container) + manual run with a real Google app.
  - Deps: T24 · Files: `vault-mcp/src/vault_mcp/oauth/upstream.py`, tests · Scope: M
  - **Status:** 17 tests against in-process mock identity providers (an OIDC provider with discovery, JWKS and RS256 ID tokens, plus a GitHub-like OAuth app with `/user/emails`); the plan mentioned the `mock-oauth2-server` container. Covered: the OIDC owner comes from the verified email; refused when the domain isn't allowed, `email_verified` is false or missing, or the nonce, signing key or audience is wrong (each audited as `oauth_login` forbidden); a callback in another browser fails; GitHub uses only the primary, verified email; the approval-code form is off when a provider is set; bad settings refuse to start. Mutation checks on email verification, nonce, domain, callback browser binding and GitHub primary selection.
  - [ ] **External check (listed for the user):** a manual sign-in with a real Google OAuth client (and optionally GitHub / Microsoft Entra apps) through a public `MCP_OAUTH_ISSUER`.

### Checkpoint D — after T24–T25
- [ ] §16.6 manual (SPEC amended 2026-11-11: the agent plugin replaces the Claude.ai connector as the client): the plugin completes both modes, scopes are enforced, and revocation works. **Done 2026-11-11, approval-code mode:** the `docs-vault` Claude Code plugin signed in on bookbotkids.com (grant `oauth:Claude-Code-plugin-docs-vault-vault-…`, write) and `get_schema` was audited under it. **Done 2026-11-17, upstream mode:** `OAUTH_UPSTREAM_PROVIDER=google` (Internal consent screen, domain bookbotkids.com); Codex signed in with a bookbotkids.com Workspace account (grant `oauth:Codex-6syn81`), a personal Gmail account was refused. Revocation exercised (`oauth:Codex-gf8jz7`, `oauth:Codex-dyt0cx`). **Open:** a read-scope sign-in.
- [x] Security review of the OAuth code (`security-auditor`) before continuing — report in `docs/reviews/2026-11-10-oauth-security-review.md` (0 critical, 0 high, 6 medium, 6 low). Fixed: M1 (parameter lengths and pending-request caps), M2 (`/authorize` errors show a page instead of redirecting), M3 (`user@host` redirect URIs refused; consent shows the hostname only and marks the name unverified), M4 (Google requires `hd`; Entra by tenant domain now works), M5 (no server-wide lockout, an alert instead), M6 (unused clients pruned after 1 h and evicted at the cap), L1 (own `/revoke` for public clients), L2 (a concurrent refresh replay revokes), L3 (security events audited; refresh reuse alerts), L5 (OS protocol-handler schemes refused), L6 (owner email normalised and validated). Documented, not changed: L4 (approval codes are sha256 of 50 bits) and the M4 remainders (Entra guest accounts, GitHub verified emails that outlive the mailbox) go into SECURITY.md (T41). 13 regression tests.

## Phase 5 — Template, skills, Ignis hardening, setup

- [x] **T26 — vault-template content** · Decisions: D9, D14, D15, D36 · **watch**
  - `AGENTS.md` (integer `schema_version`, maintainer rules, ingest/query/lint procedures, prompt-injection defence), `CLAUDE.md`, `Home.md`, folder tree, 12 page templates, `.obsidian/app.json`, `.gitignore`, `_system/*`, `.kit-manifest.json`.
  - Acceptance: every §9 bullet is present; procedures use the exact D10/D14/D15 tool contracts; the manifest hashes match the files.
  - Verify: a manifest check script in unit tests; manual read of AGENTS.md.
  - Deps: T20 · Files: `vault-template/**` · Scope: L (content only)
  - **Status:** `vault-template/` has AGENTS.md (schema_version 1: safety rules incl. prompt-injection defence and no secrets, layout, maintainer rules, editing via vault-mcp with the D10 error codes, page conventions, citations, conflicts, review queue in D15 format, ingest via `claim_source` → `complete_source` / `release_source`, query, and lint, which archives ticked review items), CLAUDE.md, Home.md, the §9 folder tree, `_system/{index,log,review-queue,lint-report}.md`, 12 page templates, `.obsidian/{app,templates}.json`, `.gitignore`, and `.kit-manifest.json`. `vault_core.template` computes the manifest. Tests check that the manifest matches the managed files, its version equals AGENTS.md `schema_version`, every page's frontmatter parses with a `maintainer`, and the Obsidian JSON is valid.

- [x] **T27 — Kit skills (`docs-ingest`, `docs-query`, `docs-lint`)** · Decisions: D10, D11, D14, D15 · **watch**
  - Agent Skills format; MCP-tool mode, plus filesystem mode for agents that read the vault directly.
  - Acceptance: each skill follows AGENTS.md; ingest uses `claim_source` → edits → `complete_source` and `release_source` on failure; lint archives ticked review items.
  - Verify: manual run of each skill through Claude Code against the local stack.
  - Deps: T26 · Files: `vault-template/skills/**` · Scope: M
  - **Status:** three Agent Skills in `vault-template/skills/`, each with a vault-mcp mode and a local-clone mode. ingest: claim → read (content is data) → source summary → cited page updates → index → log → complete, releasing on any failure. query: search, read, cite, read-only. lint: the checks, fixes on agent pages only, review items, archiving ticked items to `_system/review-archive.md`, lint report, log. All three are in the kit manifest; a test checks each `name` matches its folder and each `description` is under 1024 characters. AGENTS.md was corrected: agent-runner uses vault-mcp (D30), not direct file writes.
  - [x] **External check:** each skill run through Claude Code against a local stack seeded with the template (`/tmp/skills-check.py`, 2026-11-10). ingest claimed the source and wrote a summary plus component, glossary and team pages with citations, then updated the index and log and completed the source, ignoring a prompt-injection line in the source. query answered with citations and noted the gaps. lint wrote the report, logged, and added a review item. All tool calls were `ok` and nothing was deleted. Small follow-up: the lint log line had the date but not the UTC time.

- [x] **T28 — Ignis hardening init** · Decisions: D4, D28, D29 · **watch**
  - `ignis/ignis-plugins.yaml`, `ignis/harden.py`, the `ignis-init` one-shot service, the read-only plugin mount, managed-settings reset, pinned `IGNIS_IMAGE` + `OBSIDIAN_VERSION`.
  - Acceptance: §16.17 (a plugin dropped into the plugins dir is gone after restart and allowlisted plugins remain); a checksum mismatch aborts startup; §16.18 confirmed from the log.
  - Verify: `uv run pytest e2e -k "plugins or obsidian_version"`.
  - Deps: T1, T10 · Files: `ignis/**`, `docker-compose.yml` · Scope: M
  - **Status:** `ignis-init` (the vault-watcher image, `vault-watcher harden-ignis`) runs before Ignis (`service_completed_successfully`). It enforces `ignis/ignis-plugins.yaml` (empty by default) in `${DATA_PATH}/ignis-plugins`, which Ignis mounts read-only over `.obsidian/plugins`; resets `app.json`/`templates.json` from the template; writes `community-plugins.json`; creates the mount points; and checks the `compose.plugins.yml` data mounts for `writable_data` plugins. 13 unit tests, plus e2e in both profiles: §16.17 (a rogue plugin dropped in is gone after restart, the allowlisted plugin remains, UI installs fail on the read-only mount), a checksum mismatch makes ignis-init exit non-zero, and §16.18 (the Ignis log's Obsidian version equals `OBSIDIAN_VERSION`). Fake releases come from a test-only `plugin-stub`.

- [x] **T29 — `scripts/setup` + config examples** · Decisions: D4, D5, D16, D27, D34 · **watch**
  - `.env.example` (§13 + new vars), `vault-mcp.example.yaml`; interactive and `--non-interactive` setup; vault init/adopt; `COMPOSE_FILE` from `INGRESS` and `COMPOSE_PROFILES` for optional services; runner token; check-exposure hook (skipped until T32 exists).
  - Acceptance: a fresh `INGRESS=local` deploy on a clean VM finishes in < 30 min (§16.1 local); rerunning setup is idempotent.
  - Verify: CI runs `scripts/setup --non-interactive` in the e2e job; manual timed run on a clean VM.
  - Deps: T26, T28 · Files: `scripts/setup`, `.env.example`, `vault-mcp.example.yaml` · Scope: M
  - **Status:** `scripts/setup` (bash, shellcheck-clean): interactive prompts (current values as defaults) or `--non-interactive`; `--env-file`, `--no-start`, `--adopt`. It validates settings, derives `COMPOSE_FILE`/`COMPOSE_PROFILES`/`PUID`/`PGID`/`MCP_OAUTH_ISSUER`/`OAUTH_COOKIE_SECRET`, writes `.env` (mode 600) from `.env.example`, copies `vault-mcp.yaml`, creates the folders, copies the template into an empty vault (or adopts without overwriting), creates the agent-runner token when that profile is on, starts and waits for health, then runs check-exposure with a temporary read token. 8 e2e tests: configure, idempotent rerun, adoption, bad settings refused, ingress override and profile selection, and a full `INGRESS=local` deploy that comes up healthy.
  - [ ] **External check (listed for the user):** a timed fresh deploy on a clean VM (§16.1 local, < 30 min).

### Checkpoint E — after T26–T29
- [x] §16.1 (local), 12, 17, 18 pass; nightly-style Ignis checks run manually — full local e2e run 2026-11-10: 62 passed, 2 skipped (firewall). §16.1 local is automated by `test_16_1_local_fresh_deploy_comes_up_healthy`; the timed run on a clean VM is listed under T29. §16.17 and §16.18 pass in both profiles. §16.12 (Ignis shows an agent's edit, with no overwrite) was verified by hand in T1 (docs/LIMITATIONS.md); the nightly Playwright run comes with T38.

## Phase 6 — Ingress

- [x] **T30 — `cloudflare` profile + guide** · Decisions: D5, D39 · **watch**
  - `ingress/cloudflare/compose.yml` override (cloudflared) selected through `COMPOSE_FILE`, and `docs/ingress/cloudflare.md` (tunnel, Access app, MCP Bypass app, 50-user / 100 MB limits).
  - Acceptance: §16.1 and §16.3 for cloudflare on a real domain.
  - Verify: manual deploy; check-exposure once T32 lands.
  - Deps: T29 · Files: `ingress/cloudflare/compose.yml`, `docs/ingress/cloudflare.md` · Scope: S
  - **Status:** `ingress/cloudflare/compose.yml` (pinned `cloudflared`, a token-based remote-managed tunnel, readiness healthcheck; merges with the base file) and `docs/ingress/cloudflare.md` (tunnel public hostnames to `ignis:8080` and `vault-mcp:8082`; an Access app on the editor; an Access Bypass app plus WAF skip on the MCP host; the 50-user and 100 MB limits; §5.1 checklist; verify with check-exposure). Validated with `docker compose config` only.
  - [ ] **External check (listed for the user):** a real deploy on a Cloudflare domain: §16.1 cloudflare (< 30 min) and §16.3 (allowed user edits, disallowed user denied). **Done 2026-11-11:** deployed from Bookbot-GPU1 on `docs.bookbotkids.com` / `docs-mcp.bookbotkids.com` (Adrian's account, tunnel `docs-vault`, Access apps "Docs Vault editor" allowing `@bookbotkids.com` and "Docs Vault MCP" with Bypass). Setup, then `docker compose up` with cached images, took well under 30 min; all services healthy; `scripts/check-exposure` passes every check (certificates, editor behind login, `/mcp` 401 anonymous and accepts a bearer, OAuth metadata public, raw ports closed). §16.3 confirmed by hand the same day: a `@bookbotkids.com` account edits, a non-Bookbot account is refused.

- [x] **T31 — `caddy` profile + guide** · Decisions: D5, D39 · **watch**
  - `ingress/caddy/compose.yml` override (caddy + oauth2-proxy) selected through `COMPOSE_FILE`, plus `ingress/caddy/Caddyfile` and `docs/ingress/caddy.md`.
  - Acceptance: §16.1 and §16.3 for caddy; WebSockets work through forward_auth; `/mcp` and `/oauth/*` reachable without a browser login.
  - Verify: manual deploy on a public VM.
  - Deps: T29 · Files: `ingress/caddy/{compose.yml,Caddyfile}`, `docs/ingress/caddy.md` · Scope: M
  - **Status:** `ingress/caddy/compose.yml` (pinned Caddy 2.10.2 and oauth2-proxy v7.12.0 with healthchecks; only Caddy publishes 80/443), `Caddyfile` (`forward_auth` to oauth2-proxy with a redirect to sign-in on the editor host; WebSockets pass through; the MCP host passes through whole, with streaming flushed) and `docs/ingress/caddy.md`. Checked with `caddy validate` and `docker compose config`.
  - [ ] **External check (listed for the user):** a deploy on a public VM: §16.1 caddy and §16.3, WebSockets through forward_auth, and `/mcp` plus `/oauth/*` reachable without a browser login.

- [x] **T32 — `scripts/check-exposure`** · Decisions: D35 · **watch**
  - All §5.4 checks; PASS/FAIL/SKIP output; wired into setup.
  - Acceptance: §16.4 (passes for each profile; fails clearly against a stub proxy without auth, a fixture in e2e).
  - Verify: `uv run pytest e2e -k check_exposure`.
  - Deps: T29 · Files: `scripts/check-exposure`, `e2e/` · Scope: S
  - **Status:** `scripts/check-exposure` (bash, curl and openssl only, shellcheck-clean) runs the §5.4 checks: https for both hosts; ignis-login (the T1 Ignis markers); mcp-auth (a 401 carrying `resource_metadata`); mcp-token (from `CHECK_TOKEN`, never an argument); the oauth-metadata endpoints; and raw ports on public addresses. Each prints PASS/FAIL/SKIP and the exit code is the number of FAILs. `CHECK_SKIP` handles tailnet-only editors; `CHECK_CA_FILE` and `CHECK_PUBLIC_IPS` exist for private CAs and tests. Setup calls it with a temporary token. e2e §16.4 in both profiles: all 7 checks pass behind a TLS stand-in proxy (login wall on the editor, pass-through on the MCP host, real vault-mcp with OAuth on), and it fails with clear messages against the bare editor over plain HTTP with the raw port reachable. The e2e stack now runs with `MCP_OAUTH_ISSUER` set.

- [x] **T33 — Ingress recipe docs** · Decisions: none · **delegate**
  - `docs/ingress/{pangolin,nginx,traefik,tailscale,headscale,local}.md`, each meeting the §5.1 contract and pointing at `127.0.0.1:IGNIS_PORT` / `MCP_PORT`; note the Tailscale free plan is non-commercial.
  - Acceptance: every recipe covers all five §5.1 requirements explicitly.
  - Verify: manual review against §5.1.
  - Deps: none · Files: `docs/ingress/*.md` · Scope: M
  - **Status:** written by a delegated agent and spot-checked: six recipes (pangolin, nginx, traefik, tailscale, headscale, local). Each has a "Requirements checklist (§5.1)" table; the editor sits behind the proxy's login with WebSockets on; the whole MCP host passes without browser login. Proxy recipes use `INGRESS=none` (SPEC §5.2), `local.md` uses `local`. Open points passed to T32: check-exposure from inside a tailnet, the `local` profile, and `host:port` in `IGNIS_HOST`. Proxy configs not run against live proxies.
  - [ ] **External check (listed for the user):** a manual review of each recipe against §5.1, and a run of at least one recipe on a real host.

### Checkpoint F — after T30–T33
- [x] §16.1 (cloudflare, caddy), §16.3, §16.4 verified on real hosts — cloudflare §16.1, §16.3 and §16.4 done on bookbotkids.com (T30). **Caddy deferred by the user (2026-11-17, no urgency):** moved to Deferred below. Done locally: §16.4 e2e (pass and fail cases) and config validation of both profiles.

## Phase 7 — Optional services and lifecycle

- [x] **T34 — git-sync** · Decisions: D6, D25, D31, D32 · **watch**
  - Bash loop service (profile); lock/commit/rebase/push; conflict recovery; LFS; request-file commits; heartbeat; alerts.
  - Acceptance: §16.21 (edit reaches the remote within `SYNC_INTERVAL` + 1 min; an induced conflict → `.conflict-<ts>.md` + alert; `/data` never in the repo).
  - Verify: `uv run pytest e2e -k git_sync` with a bare local remote.
  - Deps: T23, T29 · Files: `git-sync/{Dockerfile,sync.sh}`, `scripts/lib/alert.sh`, `docker-compose.yml` · Scope: M
  - **Status:** `git-sync` (Alpine + util-linux flock, compose profile `git`, heartbeat healthcheck) runs `sync.sh`. Each cycle: lock → add → commit (named by agent-runner's request file, or `docs(auto)`) → fetch → rebase → unlock → push. On a conflict the local copy is kept as `<file>.conflict-<ts>.md`, the remote version wins the conflicting hunks, and an alert goes out; unresolvable cases pause pushing behind a `blocked` flag. Other features: LFS patterns, adopting an existing remote only with `GIT_ADOPT_REMOTE=true`, and setup generating an SSH deploy key plus `known_hosts` for ssh remotes. `scripts/lib/alert.sh` sends the same payloads as vault_core.alerts with hourly dedupe. e2e §16.21 in both profiles against a bare local remote: the edit is pushed; the request names the commit with an `Agent: agent-runner` trailer; an induced conflict gives a `.conflict` file with the local text, the remote text in place, an alert, and the copy pushed; `/data` never appears in the repo. CI shellcheck now runs with `-x`.

- [x] **T35 — agent-runner** · Decisions: D16, D30, D31 · **watch**
  - Image, supercronic schedules, the `.mcp.json` generator, job lock, budget ledger, `AGENT_COMMAND` contract, commit request files.
  - Acceptance: §16.19 (submitted source is claimed, processed, completed, and logged); §16.20 (budget reached → skip + alert); a custom command without cost output → startup warning.
  - Verify: e2e with a stub `AGENT_COMMAND` emitting `total_cost_usd`; one manual run with real Claude Code.
  - Deps: T20, T27, T34 · Files: `agent-runner/**`, `docker-compose.yml` · Scope: M
  - **Status:** image built on node 22-slim (digest-pinned) with Claude Code 2.1.322, supercronic v0.2.51 (sha256-checked per arch), jq, and the kit skills baked in as fallback. Compose profile `agent`; mounts are its state, the git-sync request folder, and the vault's `skills/` read-only (no `create_host_path`); there is no vault write access. The entrypoint generates the crontab and `mcp.json` (0600; the token never goes on the command line), installs the vault's skills over the kit's, and warns when the budget isn't enforced. `run-job` does the no-overlap `flock`, the monthly budget check (skip + alert), `claude -p --output-format json` limited to `mcp__vault__*` tools (or `AGENT_COMMAND`), records spend in `spend.json`, alerts on failure, and writes a git-sync request when git is on. e2e §16.19 and §16.20 with a scripted `AGENT_COMMAND` (node, real MCP calls): a submitted source is claimed, summarised and completed; the audit shows agent-runner's tool calls; spend and the commit request are recorded; at the budget the next job is skipped and an alert sent.
  - [x] **External check:** one real job (`/tmp/runner-check.py`, 2026-11-10; the CLI's logged-in credentials mounted for this run only). Exit 0, $0.28 recorded in `spend.json`; the source was claimed, summarised and completed with all calls through vault-mcp as `agent-runner`.

- [x] **T36 — `scripts/upgrade-vault`** · Decisions: D6, D21, D36 · **watch**
  - Schema compare; diff of AGENTS.md, templates, skills, and managed `.obsidian` files; manifest-based customisation detection; custom-block re-anchoring; `.vault-overrides`; apply on confirmation.
  - Acceptance: §16.22 (a newer template applies without touching `<!-- custom -->…<!-- /custom -->` blocks or override paths).
  - Verify: `uv run pytest vault-watcher -k upgrade` + e2e §16.22.
  - Deps: T26 · Files: `vault-watcher/src/vault_watcher/upgrade.py`, `scripts/upgrade-vault` · Scope: M
  - **Status:** `vault_watcher.upgrade` builds a plan per managed file:
    - add when the file is missing;
    - update when it is unmodified kit content from any released version, carrying custom blocks over by heading (blocks whose heading is gone are kept at the end);
    - customised (left alone, shown as a diff) when changed outside the custom blocks;
    - override when matched by `.vault-overrides` (a gitignore subset: anchoring, `dir/`, `**`, `!`).

    Changes are applied atomically under the vault lock as `upgrade-vault`. Manifest hashes now exclude custom-block contents (`kit_hash`). There is a `vault-watcher upgrade-vault [--apply]` CLI and a `scripts/upgrade-vault [--yes] [--template DIR]` wrapper. 13 unit tests with mutation checks; e2e §16.22: a newer template is applied, the custom block is kept, and the watcher records history.

- [x] **T37 — `scripts/backup` + `scripts/restore`** · Decisions: D4, D6, D37 · **watch**
  - Hot backup with SQLite online backup and a manifest; `--include-config`; restore with a stopped stack, followed by reindex.
  - Acceptance: §16.16 (backup → wipe → restore reproduces vault, history, tokens, Ignis state).
  - Verify: `uv run pytest e2e -k backup_restore`.
  - Deps: T13, T28 · Files: `scripts/backup`, `scripts/restore` · Scope: S
  - **Status:** `vault_watcher.backup` (CLI `vault-watcher backup|restore`) with host wrappers `scripts/backup [--include-config] [--out DIR]` and `scripts/restore <archive> [--force]`. Backup holds the vault lock, takes SQLite online-backup snapshots of every database, tars `vault/` + `data/` (no lock files, no WAL/SHM), optionally adds `config/`, writes `MANIFEST.json` with a sha256 per file, and leaves the archive at mode 600. Restore refuses while the stack is running, requires empty targets (or `--force`), checks every hash before writing, extracts with tarfile's `data` filter, puts config files in `./restored-config/` instead of over yours, then rebuilds the search index. 3 unit tests (a WAL-only commit survives, which the mutation check confirms; tampered archive; force) and e2e §16.16 in both profiles: backup → stop → wipe → restore → up gives back the page, the history, the old token and the Ignis state.

### Checkpoint G — after T34–T37
- [x] §16.16, 19, 20, 21, 22 green — local e2e in both profiles (`test_backup_restore`, `test_agent_runner` with a scripted AGENT_COMMAND, `test_git_sync`, `test_upgrade`). External: one agent-runner run with real Claude Code (listed under T35).

## Phase 8 — Release and docs

- [x] **T38 — Nightly Ignis e2e, release pipeline, Dependabot** · Decisions: D38 · **watch**
  - Playwright nightly for §16.12, 17, 18; `release.yml` (tag → multi-arch GHCR images); `dependabot.yml`; `SECURITY.md` disclosure contact.
  - Acceptance: the nightly runs green against pinned Ignis; a test tag publishes amd64 and arm64 images.
  - Verify: workflow runs on a fork.
  - Deps: T28, T37 · Files: `.github/workflows/{nightly,release}.yml`, `.github/dependabot.yml`, `e2e/ignis/` · Scope: M
  - **Status:**
    - `nightly.yml` (daily and on demand): the full e2e suite plus Playwright browser tests, with the `nightly` dependency group and Chromium.
    - `e2e/test_ignis_browser.py` (§16.12, marker `nightly`; regular CI excludes it): opens a note in the pinned Ignis, checks an agent's MCP append appears in the open editor, then the person's typing autosaves after it with nothing overwritten. It passes locally in both profiles. §16.17 and §16.18 already run in every e2e run.
    - `release.yml` (v* tags): QEMU + buildx builds of vault-mcp, vault-watcher, git-sync and agent-runner for amd64/arm64, pushed to `ghcr.io/<owner>/docs-vault-kit-<service>` with provenance and SBOM.
    - `dependabot.yml`: weekly updates for actions, uv, Dockerfiles and compose files; the Ignis image is excluded because it is pinned with `OBSIDIAN_VERSION`.
    - actionlint is clean. The disclosure contact is in SECURITY.md (T41).
  - [ ] **External check (listed for the user):** a nightly run on GitHub, and a test tag publishing amd64 + arm64 images (needs `packages: write` on the repository; `gh auth refresh -s write:packages` for local pushes).

- [x] **T39 — SETUP.md + UPGRADING.md** · Decisions: D5, D29, D34 · **watch**
  - Server requirements (CPU/RAM/disk, amd64/arm64), setup walkthrough per profile, upgrade procedure with the Ignis/Obsidian pin pair and a smoke test.
  - Acceptance: a person new to the kit completes §16.1 using only SETUP.md.
  - Verify: manual dry run by someone other than the author.
  - Deps: T29–T32 · Files: `docs/SETUP.md`, `docs/UPGRADING.md` · Scope: S
  - **Status:** written by a delegated agent from the code. SETUP covers requirements (all pinned images are multi-arch with arm64; sizing is measured idle memory, labelled as a starting point), setup modes, every INGRESS profile, OAuth and upstream rules, tokens, git-sync, agent-runner, alerts, backups, check-exposure and troubleshooting. UPGRADING covers kit upgrades, the Ignis/Obsidian pin pair (D29) with log verification, upgrade-vault, the automatic tokens.db migration and a smoke test. Gaps it found were fixed (setup prompts and help, `.env.example`, TARGETARCH, inotify hint).
  - [ ] **External check (listed for the user):** a dry run of §16.1 using only SETUP.md, by someone other than the author.

- [x] **T40 — USER-GUIDE.md, CONNECT-AGENTS.md, IMPORTING.md** · Decisions: D14, D15, D16, D17 · **watch**
  - The non-technical editing and review-queue guide; per-client agent setup (Claude Code, Cursor, Codex, Claude.ai, ChatGPT); the importing flow, where agents fetch content themselves (D14).
  - Acceptance: each client snippet is tested against the local stack.
  - Verify: manual connection per client.
  - Deps: T24, T27 · Files: `docs/{USER-GUIDE,CONNECT-AGENTS,IMPORTING}.md` · Scope: M
  - **Status:** written by a delegated agent from the code: the user guide (editing, maintainer, review queue, sources, history, secrets, limits), connecting agents (tokens; Claude Code, Cursor, Codex, Claude.ai and ChatGPT; skills; all 14 tools; error codes) and importing (submit_source, URL stubs, adoption, bulk ingest, prompt injection). Gaps it found were fixed in code: `scripts/oauth`, `WebFetch` for agent-runner, ingest caps in compose, and the review-item text. Still open: Claude.ai's "published identity" option isn't supported, so use dynamic registration.
  - [ ] **External check (listed for the user):** connect each client by hand. **Done 2026-11-10:** Claude Code (2.1.285) and Codex CLI (0.167.0; the snap 0.114 is too old for the current model list) both pass, with each step `ok` and audited (`/tmp/codex-check.py`). The docs-vault plugin with OAuth passes in Claude Code, Codex and omp (2026-11-11), each an audited grant. **Open:** Cursor and ChatGPT (optional). The user dropped the Claude.ai check: the plugin is the main sign-in path.

- [x] **T41 — SECURITY.md + LIMITATIONS.md (final)** · Decisions: D6, D16, D17, D20, D24, D28 · **watch**
  - Threat model, Ignis having no auth, plugin allowlist, token/OAuth lifecycle, secrets, offboarding checklist; LIMITATIONS covering T1 results, the Ignis/MCP write race, web attribution, Ignis legal basis, Obsidian terms.
  - Acceptance: every §14 bullet for both docs is covered; the offboarding checklist revokes bearer tokens + OAuth grants + IdP access.
  - Verify: `security-auditor` review of the docs against the code.
  - Deps: T1, T24, T28 · Files: `docs/SECURITY.md`, `docs/LIMITATIONS.md` · Scope: S
  - **Status:** written by a `security-auditor` agent, checked against the code and the pinned Ignis image (WS_ORIGINS is enforced; cross-site writes are refused).
    - SECURITY.md covers the threat model, Ignis having no auth, the plugin allowlist and Ignis server plugins, bearer and OAuth lifecycles with the Checkpoint D remainders, secrets, rate limits, audit, symlinks, data at rest, disclosure via GitHub private vulnerability reporting, and an offboarding checklist (`scripts/token revoke`, `scripts/oauth revoke-codes`, IdP and ingress removal).
    - LIMITATIONS.md keeps the T1 results and adds the write race, attribution, restricted mode, symlinks, search, the Cloudflare/Tailscale limits, and the Ignis AGPL licence and Obsidian terms.
    - Gaps it found were fixed in code: the Ignis server-plugin reset plus alert, admin audit, revoke-codes, and the `vault-mcp-token` rule. The rest are documented.
  - [ ] **External check (listed for the user):** enable private vulnerability reporting in the GitHub repository settings.

### Checkpoint H — complete
- [x] All 22 §16 acceptance tests pass (Caddy §16.1/§16.3 and the other items under Deferred were moved out of v0.1.0 by the user) (CI e2e + nightly + documented manual runs) — **CI (PR #1, run 42767114630):** lint, unit, and e2e 76 passed / 1 skipped (the nightly browser test, which runs in nightly.yml). **Local:** unit 520+, e2e 74 passed / 2 skipped (host firewall) in both profiles, nightly browser test passes locally. Automated locally: §16.2, 4, 5, 7–11, 13–22 and the §16.1 local deploy (§16.12 via the nightly test). **External, listed per task:** §16.1 cloudflare/caddy and a timed clean-VM run, §16.3 on real hosts, §16.6 via the agent plugin (approval-code mode done), manual client connections, a real Claude Code agent-runner run, the nightly and release workflows on GitHub.
- [x] CHANGELOG updated; v0.1.0 tag published — `[0.1.0] - 2026-11-17` in CHANGELOG; PR #1 merged (`d2d7242`); tag `v0.1.0` pushed; release run 44008227359 published vault-mcp, vault-watcher, git-sync and agent-runner to GHCR for linux/amd64 and linux/arm64. Private vulnerability reporting is on.

## Deferred (no urgency; the user chose to release without them, 2026-11-17)

- [ ] **Caddy profile on a public host** (T31, §16.1 caddy, §16.3): a cloud VM (e.g. Hetzner CX23, ~€0.01/hour) or 80/443 forwarded to Bookbot-GPU1. Bookbot-GPU1 is not reachable on 80/443 today (tested with check-host.net). Steps: docs/ingress/caddy.md, then scripts/check-exposure and the allowed/denied login check.
- [ ] Timed fresh install on a clean machine (§16.1 local, < 30 min; T29).
- [ ] A dry run of SETUP.md by someone other than the author (T39).
- [ ] A read-scope OAuth sign-in, to confirm read access can't write (§16.6).
- [ ] Cursor and ChatGPT clients (optional; T40).
- [ ] Manual review of the ingress recipes against §5.1 (T33).


---

# v0.2 — Second brain

Source: `SPEC.md` §18 + `SPEC-<module>.md`. Decisions D40–D46 in `tasks/plan.md` § v0.2. Module id in brackets after each task.

## Phase 9 — Spike

- [x] **T42 — Harness hook spike** [session-hooks] · Decisions: D44 · **watch**
  - In Claude Code, omp and Codex CLI, install a throwaway plugin whose `SessionStart` hook prints a marker context block, plus a turn-end and a pre-compact hook that log to a file. Record in `tasks/impl-notes.md`: does the context reach the model, which events fire, how each harness loads plugin hooks (omp: `omp.extensions` module vs Claude-hook compatibility; Codex: plugin `hooks/hooks.json` after `/hooks` trust), and the output schema each expects.
  - Acceptance: a per-harness table of what works, and the chosen wiring for each; Codex marked supported or not for v0.2.
  - Verify: the marker appears in each harness's first answer; logs show the events.
  - Deps: none · Files: throwaway (deleted), `tasks/impl-notes.md` · Scope: S
  - **Status:** done. Claude Code (`hooks/hooks.json`: SessionStart + UserPromptSubmit context) and omp (`hooks/pre/*.ts`, `before_agent_start` message) both inject context from an installed plugin. Codex CLI 0.167–0.169 ignores plugin hooks, so Codex is out for v0.2. Details and wiring in impl-notes.

## Phase 10 — write-policy

- [x] **T43 — Policy model and decision order** [write-policy] · Decisions: D40 · **watch**
  - `WritePolicy`: `protected`, `human_pages: read_only|edit_with_notice`, `human_edit_grace_s`; `check_write` order per SPEC-write-policy; new reasons `protected`, `recently_edited`.
  - Acceptance: a table test covers every row of the decision order in both modes; `protected` matches files and folders; v0.1 default config still loads.
  - Verify: `uv run pytest vault-core -k policy`.
  - Deps: none · Files: `vault-core/src/vault_core/policy.py`, `vault-core/tests/test_policy.py`, `vault-mcp.example.yaml` · Scope: S
  - **Status:** `WritePolicy` gains `human_pages: edit_with_notice`, `protected` (files or `folder/` prefixes; defaults to the rule files, so v0.1 policy files get the protection too) and `human_edit_grace_s` (default 90). `check_write(…, web_saved_s_ago=)` follows the D40 order and returns `Decision.human_page=True` for allowed human-page edits. 21 new test cases (decision table, v0.1 file loading, field validation). The example policy is now the v0.2 one, so the e2e harness runs with it (the §16.10 refusal test moves to `read_only` mode in T45).

- [x] **T44 — Writer: grace, summary, human-page notice** [write-policy] · Decisions: D40 · **watch**
  - Latest-web-history lookup; `summary` on all write tools (required on human pages); `human_page: true` in audit; daily notice deduplicated in `state.db`; `move` checks `protected` on both paths.
  - Acceptance: SPEC-write-policy criteria 2, 4, 5 pass in unit tests with recent/expired history timestamps; v0.1 human-page tests run in `read_only` mode and pass.
  - Verify: `uv run pytest vault-mcp -k "write or human or protected or grace"`.
  - Deps: T43 · Files: `vault-mcp/src/vault_mcp/writer.py`, `vault-mcp/src/vault_mcp/server.py`, `vault-core/src/vault_core/review_queue.py`, tests · Scope: M
  - **Status:** `write`, `append`, `replace_section`, `insert_after` and `move` take an optional `summary` (≤ 200 chars; required on `maintainer: human` pages, `invalid` otherwise), stored in the audit line with `human_page: true`. Grace: the most recent `web` version of the file in history (not just the head: a later scan or agent write mustn't hide it) younger than `human_edit_grace_s` → new error code `recently_edited` with `retry_after_s`; it also blocks a move when the page or any linking page was just saved. Notices: `#review/agent-edit` item, one per page per UTC day, claimed in `state.db` (`vault_mcp/notices.py`), released if the queue write fails; a move notifies person pages whose links it rewrote. `protected` refusals reuse `forbidden` with `reason: protected`. 6 new tests against a real watcher; the v0.1 tests run with `human_edit_grace_s: 0` in their read-only fixture policy. 546 unit tests pass.

- [x] **T45 — write-policy e2e and docs** [write-policy] · Decisions: D40 · **delegate**
  - e2e for criteria 1–6 in both profiles; SECURITY.md (protected files, injection risk now covers human docs), UPGRADING.md (new policy block), CONNECT-AGENTS.md error codes (`recently_edited`, `protected`).
  - Acceptance: e2e passes; the full v0.1 e2e suite still passes.
  - Verify: `uv run pytest e2e -k "write_policy" -m "(profile_local or profile_none) and not nightly"`.
  - Deps: T44 · Files: `e2e/test_write_policy.py`, `docs/SECURITY.md`, `docs/UPGRADING.md`, `docs/CONNECT-AGENTS.md` · Scope: M
  - **Status:** `e2e/test_write_policy.py`: 10 passed across local/none profiles (including actual 90 s grace expiry and legacy `read_only` refusal); focused ruff and format checks passed. Retained full non-nightly e2e: 81 passed, 2 host-firewall skips, 2 nightly deselected; vault-mcp 238 passed; ruff and mypy passed before resume. SECURITY, UPGRADING, CONNECT-AGENTS, USER-GUIDE and changelog updated. D40/SPEC-write-policy clarify latest-web lookup and stale-base conflict precedence. Live deployment unchanged.

### Checkpoint I — after T43–T45
- [x] SPEC-write-policy success criteria 1–6 pass; lint, unit and e2e green; human review of the decision order.
  - Approved by the user after reviewing Checkpoint I. Phase 11 may proceed; live rollout still requires T58 approval.

## Phase 11 — knowledge-graph

- [x] **T46 — Graph model: ontology, extraction, validation** [knowledge-graph] · Decisions: D41, D42 · **watch**
  - `vault_core.graph`: ontology model and loader; `extract()` (typed, mentions, sourced_from, summarizes_raw, belongs_to_project, has_repo); `validate()`; starter `vault-template/_system/graph/ontology.yaml`.
  - Acceptance: fixtures produce the expected nodes and edges; each validation error kind is reported with its field.
  - Verify: `uv run pytest vault-core -k graph`.
  - Deps: none · Files: `vault-core/src/vault_core/graph.py`, `vault-core/tests/test_graph.py`, `vault-template/_system/graph/ontology.yaml` · Scope: M
  - **Status:** pure dataclass model, validated ontology loader/starter, explicit and implicit extraction, source-summary/ADR compatibility, original relationship field indexes. Core graph tests pass as part of the 599-test regression run; schema-2 managed-template registration remains T52.

- [x] **T47 — Graph store in vault-watcher** [knowledge-graph] · Decisions: D41, D42 · **watch**
  - `graph.sqlite` schema; per-page update in the index pass (edit, rename, delete); `reindex` rebuilds it; ontology reload with alert on an invalid file.
  - Acceptance: incremental result equals a full rebuild; `read.deny` pages never compiled; 5,000 pages / 50,000 edges rebuild < 60 s.
  - Verify: `uv run pytest vault-watcher -k graph`.
  - Deps: T46 · Files: `vault-watcher/src/vault_watcher/graph_store.py`, `vault-watcher/src/vault_watcher/{recorder,cli}.py`, tests · Scope: M
  - **Status:** transactional SQLite rebuild and incremental updates wired to recorder, startup, reindex, history restore and backup restore. Actual source-summary metadata is revalidated for dependent pages; ontology reload retains the persisted last good vocabulary and alerts without quoting invalid content. Rebuild proof: 5,000 nodes/50,000 edges in 0.956 s on this host. CI now runs the same <60 s gate.

- [x] **T48 — Graph tools and write-time validation** [knowledge-graph] · Decisions: D41 · **watch**
  - `graph_neighbors`, `graph_edges`, `graph_path`, `backlinks`, `graph_lint` with permission filtering; agent writes with an invalid `graph:` block → `invalid` + `graph_errors`.
  - Acceptance: SPEC-knowledge-graph criteria 2–4 in unit tests, including a folder-scoped credential and a `read.deny` endpoint; depth-2 neighbours < 200 ms on the 50k-edge fixture.
  - Verify: `uv run pytest vault-mcp -k graph`.
  - Deps: T47 · Files: `vault-mcp/src/vault_mcp/tools_graph.py`, `vault-mcp/src/vault_mcp/{server,writer}.py`, tests · Scope: M
  - **Status:** all five read-scope tools registered; declaring-page/endpoints/intermediates filtered before limits, source/evidence redacted together, ambiguous ids/aliases not chosen. Writer validation uses current readable source metadata; moves rewrite graph/frontmatter references as well as body links. 599 unit tests pass, including cold/warm 5k/50k query checks below 200 ms; ruff/format/mypy (45 files) pass. CI runs the query budget too.

- [x] **T49 — knowledge-graph e2e and docs** [knowledge-graph] · **delegate**
  - e2e for criteria 1–6 (including backup → restore → reindex graph counts); CONNECT-AGENTS tools table; SETUP reindex note.
  - Verify: `uv run pytest e2e -k graph -m "(profile_local or profile_none) and not nightly"`.
  - Deps: T48 · Files: `e2e/test_graph.py`, `docs/CONNECT-AGENTS.md`, `docs/SETUP.md` · Scope: S
  - **Status:** all four profile scenarios passed: local write/query/permission/move/lint on the first run, then both corrected restore fixtures and the none-profile browser-debounce wait on the focused retry (3 passed, 1 unchanged scenario deselected). All six graph success criteria covered. CONNECT-AGENTS, SETUP, changelog and graph spec updated; no live deployment.

### Checkpoint J — after T46–T49
- [ ] SPEC-knowledge-graph criteria 1–6 pass; lint, unit and e2e green.
  - Automated acceptance complete; **human review pending** before Phase 12. Review full-path derived ids versus stable explicit ids, source provenance versus quote truth, and schema-1/default-ontology compatibility. Local checks passed; GitHub CI performance gates are configured, not yet observed on the runner.

## Phase 12 — doc-authoring

- [ ] **T50 — Repo identity and `project_context`** [doc-authoring] · **watch**
  - `normalise_remote()`; `project_context(repo?, project?)` (project page summary and status, 1-hop graph neighbourhood, open review items for those pages, last 5 change-log lines; ≤ 4,000 chars; permission-filtered).
  - Acceptance: SPEC-doc-authoring criterion 2; unknown repo → `{project: null}`.
  - Verify: `uv run pytest vault-core vault-mcp -k "remote or project_context"`.
  - Deps: T48 · Files: `vault-core/src/vault_core/repos.py`, `vault-mcp/src/vault_mcp/tools_project.py`, `vault-mcp/src/vault_mcp/server.py`, tests · Scope: M

- [ ] **T51 — Skills: ingest, query, lint, capture** [doc-authoring] · **watch**
  - Update `docs-ingest` (human docs after the wiki step), `docs-query` (graph first), `docs-lint` (human-doc drift, graph lint); new `docs-capture` (full in-session ingest per finding). Plugin commands `document`, `ingest`, `ask`, `lint` in `agent-plugin/commands/`, each a thin wrapper over the skills, acting immediately.
  - Acceptance: each skill names tools without prefixes and follows SPEC-doc-authoring's writing rules; `scripts/agent-plugin` copies the new skill and the four commands; generated plugin validates.
  - Verify: `uv run pytest e2e/test_agent_plugin.py`; `claude plugin validate` on the generated plugin.
  - Deps: T50 · Files: `vault-template/skills/*/SKILL.md`, `agent-plugin/commands/{document,ingest,ask,lint}.md`, `e2e/test_agent_plugin.py` · Scope: M

- [ ] **T52 — Template schema 2** [doc-authoring, write-policy, knowledge-graph] · Decisions: D43, D36 · **watch**
  - AGENTS.md v2 (layout, `maintainer` meanings, writing rules, typed edges, capture); templates with `repo:`, `project:`, `graph`, `## Change log`; `.kit-manifest.json`; `upgrade-vault` from 1 to 2 keeps `<!-- custom -->` blocks.
  - Acceptance: SPEC-doc-authoring criterion 4; a fresh vault gets schema 2.
  - Verify: `uv run pytest vault-watcher -k upgrade` + `uv run pytest e2e -k upgrade`.
  - Deps: T51 · Files: `vault-template/AGENTS.md`, `vault-template/_system/templates/*`, `vault-template/.kit-manifest.json`, upgrade tests · Scope: M

- [ ] **T53 — doc-authoring e2e and user docs** [doc-authoring] · **delegate**
  - Scripted-agent e2e: new pipeline source → `pipelines/<name>.md`, project link, `documented_in` edge; second source updates one section + one change-log line; the `document` and `ingest` flows finish in the same run with nothing left in `raw/`. USER-GUIDE (where things live now, agent edits, notices), IMPORTING (repo summaries).
  - Verify: `uv run pytest e2e -k doc_authoring -m "(profile_local or profile_none) and not nightly"`.
  - Deps: T52 · Files: `e2e/test_doc_authoring.py`, `docs/USER-GUIDE.md`, `docs/IMPORTING.md` · Scope: M

### Checkpoint K — after T50–T53
- [ ] SPEC-doc-authoring criteria 1–4 pass; lint, unit and e2e green.

## Phase 13 — session-hooks and scheduled-maintenance (parallel)

- [ ] **T54 — Hook scripts** [session-hooks] · Decisions: D44 · **watch**
  - `agent-plugin/hooks/session-start.sh`, `prompt-nudge.sh` (every 10th prompt), re-injection after compaction; repo detection, allowlist, opt-outs, 1,500-char cap, per-harness output from T42.
  - Acceptance: fixture tests for allowed/not-allowed, ssh/https, no remote, `.docs-vault-ignore`, `DOCS_VAULT_CAPTURE=off`, nudge cadence, missing `jq`/git (exit 0, no output); omp module `hooks/pre/docs-vault.ts` injects once per session.
  - Verify: `uv run pytest e2e/test_hooks.py`; shellcheck.
  - Deps: T42, T50 · Files: `agent-plugin/hooks/*.sh`, `e2e/test_hooks.py` · Scope: M

- [ ] **T55 — Plugin generator: hooks and allowlist** [session-hooks] · Decisions: D44 · **watch**
  - `scripts/agent-plugin --capture-repos`; per-harness wiring per T42; plugin README; CONNECT-AGENTS section on automatic behaviour, opt-out and Codex hook trust.
  - Acceptance: generated output validates (`claude plugin validate`), contains each harness's hook wiring and the baked allowlist; existing generator tests pass.
  - Verify: `uv run pytest e2e/test_agent_plugin.py`.
  - Deps: T54 · Files: `scripts/agent-plugin`, `e2e/test_agent_plugin.py`, `docs/CONNECT-AGENTS.md` · Scope: M

- [ ] **T56 — `scripts/maintenance`** [scheduled-maintenance] · Decisions: D45 · **watch**
  - `--setup` (token), run (MCP config, `claude -p` with `mcp__vault__*` only, budget, timeout, outcome check, alert), `--dry-run`, `MAINTENANCE_AGENT_COMMAND`.
  - Acceptance: stub-agent e2e for ok, failing agent, no report, unreachable vault-mcp, dry-run; exactly one `maintenance_failed` alert on the webhook stub per failure.
  - Verify: `uv run pytest e2e -k maintenance -m "(profile_local or profile_none) and not nightly"`; shellcheck.
  - Deps: T51 · Files: `scripts/maintenance`, `e2e/test_maintenance.py` · Scope: M

- [ ] **T57 — Maintenance prompt and docs** [scheduled-maintenance] · **delegate**
  - The run prompt (ingest, structural, semantic, fix, report) aligned with `docs-lint`/`docs-ingest`; SETUP "Nightly maintenance" (host cron after backup; agent-runner as the container alternative); SECURITY (maintenance token).
  - Deps: T56 · Files: `scripts/maintenance` (prompt block), `docs/SETUP.md`, `docs/SECURITY.md` · Scope: S

### Checkpoint L — after T54–T57
- [ ] SPEC-session-hooks criteria 4–5 and SPEC-scheduled-maintenance criteria 3–4 pass in automated tests; lint, unit and e2e green.

## Phase 14 — Bookbot rollout and v0.2.0

- [ ] **T58 — Deploy v0.2 to Bookbot-GPU1** · Decisions: D46 · **watch** (ask before each live step)
  - Backup; pull/build images; `upgrade-vault` to schema 2; new policy (old kept as `vault-mcp.yaml.v1`); reindex; check-exposure; smoke test through MCP (graph tools, `project_context`).
  - Acceptance: services healthy; check-exposure passes; existing grants still work.
  - Deps: Checkpoint K · Scope: S

- [ ] **T59 — Plugin with hooks, live** [session-hooks] · **watch**
  - `scripts/agent-plugin --capture-repos 'github.com/bookbot-hive/*,github.com/bookbot-kids/*'`; push to `bookbot-hive/bookbot-doc-vault`; update Claude Code, omp (and Codex if supported).
  - Acceptance: SPEC-session-hooks criteria 1–3 and 6 in real sessions (a mid-session finding fully ingested before the next answer); SPEC-doc-authoring criterion 5 (`/docs-vault:document`, `ingest`, `ask`, `lint` in Claude Code and omp); audit shows the user's grant.
  - Deps: T55, T58 · Scope: S

- [ ] **T60 — Nightly maintenance, live** [scheduled-maintenance] · **watch**
  - `scripts/maintenance --setup`; one `--dry-run` reviewed with the user; one real run reviewed; wire into `~/docs-vault-ops/nightly-backup.sh` after the backup; one deliberately failed run to check the Slack alert.
  - Acceptance: SPEC-scheduled-maintenance criteria 1, 2, 4; report reviewed by the user.
  - Deps: T57, T58 · Scope: S

- [ ] **T61 — Release v0.2.0** · **watch** (tag needs the user's go-ahead)
  - CHANGELOG `[0.2.0]`, versions, README status, `SPEC.md` status; CI green; tag after go-ahead; GHCR images checked.
  - Deps: T59, T60 · Scope: S

### Checkpoint M — v0.2 complete
- [ ] All five module specs' success criteria pass (automated + documented live runs); v0.2.0 released.
