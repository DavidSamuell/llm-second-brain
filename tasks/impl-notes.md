# Implementation notes: Docs Vault Kit
Spec: `SPEC.md` (Draft v0.4)  Plan: `tasks/plan.md`  Tasks: `tasks/todo.md`  Started: 2026-11-09

**Summary:** no code written yet. The entries below were captured during planning (they reference the plan's D-numbers). The deviation the reviewer must look at first is **D21**: MCP writes fail closed while vault-watcher is down.

## Decisions (not covered by the spec)
Full rationale for each is in `tasks/plan.md` under the D-number. This list is only the index. Reversible = can be changed after release without migrating data or breaking clients.

- [T5] D1: Python 3.13, uv workspace, ruff, mypy --strict, pytest, argparse CLIs. Why: one lockfile, no extra CLI dependencies. Reversible: yes.
- [T10] D3: all kit containers run as `${PUID}:${PGID}`, the same ids as Ignis, with a single uvicorn worker. Why: shared file ownership in `/vault`, and rate-limit and lease state live in memory. Reversible: yes.
- [T10, T29] D5: ingress is chosen by setup writing `COMPOSE_FILE` (base + `ingress/<name>/compose.yml`) into `.env`; git-sync and agent-runner use compose `profiles`. Why: compose `include:` has interpolation bugs, and plain `docker compose up` must keep working. Reversible: yes.
- [T7] D6: the advisory lock is `flock(2)` on `/data/locks/vault.lock`, with the holder's name in `vault.owner`. Why: works across containers, interoperates with `flock(1)`, and is released when a process crashes. Reversible: yes until git-sync ships; after that both sides must change together.
- [T7] D7: `version` is the lowercase hex sha256 of raw bytes, `null` when the file doesn't exist; writes use a same-directory temp file + `os.replace`. Why: atomic and independent of mtime. Reversible: **no** once clients store versions.
- [T6] D8: `--folders` limits read, list, search, and write, using case-sensitive prefix matching. Reversible: yes.
- [T6] D9: the maintainer comes from the file **on disk**, not from the incoming content; a new file only has to pass `write.allow`. Reversible: yes.
- [T10] D10: errors are `ToolError` with JSON `{code, message, details}` and a fixed list of codes. Reversible: **no** once agents and skills parse the codes.
- [T17] D11: `heading` accepts the bare text or the full `## line`, matching exactly one heading; `insert_after` inserts directly below the heading line. Reversible: no (it changes where content lands).
- [T10] D13: non-UTF-8 files are returned as `content_base64` + `mime`; write tools accept text only. Reversible: yes.
- [T20] D14: leases are stored in `/data/state.db`, a file not listed in §4. Submitted sources are named `raw/<date>-<slug>.md`. Reversible: yes.
- [T18] D15: the review queue is Obsidian tasks in `_system/review-queue.md` with `#review` / `#review/high` tags and `^rq-<id>` block ids. Reversible: no, without migrating existing items.
- [T9] D16: tokens are `vmcp_` + 32 bytes, stored as sha256 hashes; `--expires` defaults to `90d`, and `never` must be explicit. Reversible: the default yes; the token format no.
- [T24] D17: a subclass of FastMCP `OAuthProvider` on `tokens.db`; opaque tokens (access 1 h, refresh 30 d with rotation); scopes `vault:read` / `vault:write`. Reversible: partly; changing scope names breaks existing grants.
- [T25] D18: authlib for upstream login; GitHub only via its verified-email API; upstream tokens are discarded. Reversible: yes.
- [T21] D19: rate-limit counters live in memory, so `writes_per_day` resets when vault-mcp restarts. Reversible: yes.
- [T8] D20: every tool call is audited, reads included; no log rotation in v1. Reversible: yes.
- [T14] D23: FTS5 with `unicode61 remove_diacritics 2` and no stemming; user terms are quoted; `read.deny` paths are never indexed. Reversible: yes (reindex).
- [T8] D24: curated regex rules from gitleaks' high-confidence set, no entropy rules, and no allowlist bypass. Reversible: yes.
- [T8] D25: the webhook payload shape is picked from the URL host (Slack, Discord, or generic). Reversible: yes.
- [T23] D26: the watcher monitors peers through `/health` and heartbeat files, with no Docker socket. Reversible: yes.
- [T6, T29] D27: `vault-mcp.yaml` is generated from `vault-mcp.example.yaml` and gitignored; changes need a restart. Reversible: yes.
- [T35] D30/D31: agent-runner is Node + pinned Claude Code + supercronic; budget comes from `total_cost_usd`; commit messages are handed to git-sync through `/data/git-sync/requests/`. Reversible: yes.
- [T35] D30: agent-runner writes only through vault-mcp and has no vault write mount. This fits the spec: vault-mcp's write path takes the shared §6.6 lock for every runner write, and §9 offers the filesystem as an option, not a requirement. Why: policy, audit, conflict checks, and the D21 history handoff apply to the runner too. Reversible: yes.
- [T34] D32: on conflict git-sync runs `rebase --abort`, saves `.conflict-<ts>.md` copies, then `rebase -X ours` (the remote wins conflicting hunks). Modify/delete conflicts stop pushing. Reversible: yes.
- [T29, T32] D33–D35: host scripts are bash, with Python logic inside the containers; check-exposure reads its token from the `CHECK_TOKEN` env var. Reversible: yes.
- [T36] D36: integer `schema_version`, paired `<!-- custom -->…<!-- /custom -->` markers, a hash manifest, and gitignore-syntax `.vault-overrides`. Reversible: no once vaults carry the markers.
- [T37] D37: hot backup using the SQLite online backup API; restore requires the stack stopped; config files are included only with `--include-config`. Reversible: yes.
- [T38] D38: Dependabot (not Renovate); Playwright for the nightly Ignis checks. Reversible: yes.
- [T30] D39: the Cloudflare tunnel is remotely managed via `CF_TUNNEL_TOKEN`; Access is configured by following the docs, not the API. Reversible: yes.

## Deviations (conservative option taken; needs human review)
At this stage the location is the plan line where the deviation is decided. Code locations get added as tasks land.

- [T5] Spec said: §14 layout lists only `vault-mcp/` and `vault-watcher/`. → Did: added a third package, `vault-core/`, shared by both. Because: the secret rules, the lock, the history format, and `read.deny` must be identical in both services (§16.13). — `tasks/plan.md:26`
- [T10, T37] Spec said: §12 back up the "Ignis data volume". → Did: Ignis `/app/data` is bind-mounted to `${DATA_PATH}/ignis`; only the re-downloadable Obsidian cache stays a named volume. Because: backup then covers just `/vault` + `/data`. — `tasks/plan.md:38`
- [T13, T16] Spec said: §6.6/§7.1 history is "handled by vault-watcher" and stores "the previous version". → Did:
  - The watcher remains the only writer of history; vault-mcp only spools prepared/committed transactions into `/data/history/.inbox/`.
  - The watcher records the **new** content on every change, plus a startup baseline. This is equivalent coverage, but the wording differs.
  - **MCP writes fail closed with `history_pending` while the watcher is down.**
  Because: this is the only way to guarantee §16.15 across crashes. — `tasks/plan.md:206`
- [T16] Spec said: §6.5 `secrets.reject_on_write: true`. → Did: only lines **added or changed** against the current file are scanned. Because: a secret already sitting in a human page would otherwise block every append to that file. Risk: a full `write` that keeps an existing secret line unchanged is accepted (the watcher has already flagged that line). — `tasks/plan.md:267`
- [T19] Spec said: §6.4 `move` = "rename + update wikilinks". → Did: the move is rejected outright (`forbidden`, nothing changed) when any backlink sits in a file the credential can't write. Because: §6.5 `human_pages: read_only` forbids editing those files, and a partial move would leave stale links. — `tasks/plan.md:112`
- [T20] Spec said: §6.4 `submit_source(title, content | url, origin)`. → Did: a `url` is stored as a stub and never fetched by the server. Because: server-side fetching means SSRF and egress risk, and §10.1 already has agents fetching sources themselves. — `tasks/plan.md:137`
- [T28] Spec said: §8 plugins are "pre-installed from the template". → Did: `ignis-init` downloads the pinned GitHub release assets, verifies their sha256, and caches them in `${DATA_PATH}/ignis-plugins`; nothing is vendored in `vault-template/`. Because: avoids redistributing third-party plugin code and its licences. — `tasks/plan.md:296`
- [T29] Spec said: §13 is the complete `.env.example`. → Did: added `VAULT_NAME`, `PUID`, `PGID`, `COMPOSE_FILE`, `COMPOSE_PATH_SEPARATOR`, `COMPOSE_PROFILES`, `OAUTH_UPSTREAM_ISSUER`, `HISTORY_ACK_TIMEOUT`. Because: each one is needed by D4/D3/D5/D18/D21. — `tasks/plan.md:440`
- [T29] Spec said: §8 `OBSIDIAN_VERSION` is pinned and "Ignis downloads this exact version". → Did: set it to exactly the version pinned by the `IGNIS_IMAGE` digest. Because: Ignis rejects anything older than its own pin and doesn't guarantee newer versions. — `tasks/plan.md:305`

## Surprises (things learned)
- The repo started with only `SPEC.md`, `README.md` (18 B), and a stock Python `.gitignore`, so there are no existing conventions to follow.
- Ignis expects vaults under `VAULT_ROOT=/vaults/<name>` (one sub-folder per vault), listens on 8080 internally, keeps its state in `/app/data`, and caches Obsidian in `/app/obsidian-app`. Source: https://ignis.thiefling.com/docs/server/deploy/
- Ignis `OBSIDIAN_VERSION` only accepts versions **at or above** the one Ignis pins. Ignis enforces no write protection and has no auth, but it has `WS_ORIGINS` for Origin allowlisting. Source: https://ignis.thiefling.com/docs/server/environment/
- FastMCP `OAuthProxy` keeps clients and tokens in its own encrypted key-value store and passes upstream tokens through, so it can't satisfy §6.3's single `tokens.db`. The abstract `OAuthProvider` hands all storage to the subclass. Source: https://gofastmcp.com/docs/v3/servers/auth/full-oauth-server.md
- Docker Compose `include:` has open and recent interpolation bugs (docker/compose #12496, #13540, #13644), which is why D5 uses `COMPOSE_FILE`.

## Open questions for the human
- [ ] D2: is adding `vault-core/` to the §14 layout acceptable?
- [ ] D4/D37: is it OK to keep Ignis state in `${DATA_PATH}/ignis` instead of a Docker volume?
- [ ] D14: OK that `submit_source(url=…)` creates a stub and never fetches?
- [ ] D21: is it acceptable that MCP writes fail closed (`history_pending`, nothing changed) while vault-watcher is down?
- [ ] D24: is diff-only secret scanning on MCP writes acceptable, or must every `write` re-scan the whole file even when that blocks edits to pages that already contain a secret?
- [ ] §13: approve the 8 new env vars.
- [ ] §8 / D28: is downloading plugins from GitHub at startup OK, instead of shipping them in the template? (This needs network access on the first start.)
