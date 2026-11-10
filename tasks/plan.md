# Implementation Plan: Docs Vault Kit (SPEC.md v0.4)

## Overview

Greenfield build of the kit in SPEC.md: two Python services (`vault-mcp`, `vault-watcher`) plus a shared library, the Ignis hardening init, git-sync, agent-runner, ingress profiles, vault template + skills, operator scripts, docs, and CI. The repo has only SPEC.md, README.md, and a Python `.gitignore`, so there is no existing code to follow.

Build order: risky unknowns first (Ignis behaviour, FastMCP OAuth), then the shared core, then the MCP read path, write path, watcher, OAuth, hardening/template, ingress, optional services, and release. Each phase ends with a checkpoint tied to SPEC §16 acceptance tests.

Tasks with acceptance criteria are in `tasks/todo.md`. Each task names the decisions below that it relies on.

External facts checked while planning:
- Ignis image `nobbe/ignis` (amd64/arm64). Defaults: `PORT=8080`, `VAULT_ROOT=/vaults` (one sub-folder per vault), `DATA_ROOT=/app/data`, Obsidian cache at `/app/obsidian-app`. Supports `PUID`/`PGID` and `WS_ORIGINS`. `OBSIDIAN_VERSION` only accepts versions **at or above** the one Ignis pins. Ignis has no auth and no write protection. Sources: https://ignis.thiefling.com/docs/server/deploy/, https://ignis.thiefling.com/docs/server/environment/
- FastMCP ships an abstract `OAuthProvider` (full authorization server; storage delegated to the subclass) and an `OAuthProxy` (DCR facade over upstream IdPs, own encrypted KV store). Sources: https://gofastmcp.com/docs/v3/servers/auth/full-oauth-server.md, https://gofastmcp.com/docs/v3/servers/auth/oauth-proxy.md
- Compose `include:` has open and recent interpolation bugs (docker/compose #12496, #13540, #13644).

## Architecture Decisions

Format for each decision: **Choice** / **Rejected** / **Why**. "Spec" lists the sections the decision fills in. A ⚠ marks a decision that stretches the spec's wording. These are repeated under Open Questions.

### D1 — Python toolchain
- Spec: §15, §17 (Python, FastMCP, watchdog, SQLite FTS5).
- **Choice:** Python 3.13 and a `uv` workspace with a committed `uv.lock`. Lint with `ruff` (lint + format), type-check with `mypy --strict`, test with `pytest` + `pytest-asyncio`. CLIs use stdlib `argparse`. Env config uses `pydantic-settings` and the YAML policy uses a `pydantic` model, since both are already in FastMCP's dependency tree. Pin `fastmcp` to the 3.x minor in use. (T2: that is `fastmcp==3.4.11` with mcp SDK 1.30.0. A 4.x line also exists (4.1.6, mcp SDK 2.3.0 on `httpx2`); moving to it is an open question in impl-notes, not part of this plan.)
- **Rejected:** Poetry or pip-tools; Typer or Click for CLIs.
- **Why:** uv gives one lockfile across the three packages and fast, reproducible Docker builds. argparse covers `token create|list|revoke` and `oauth approve-code` without adding dependencies.

### D2 — Shared `vault-core/` package ⚠
- Spec: §14 layout, §6.6, §7.
- **Choice:** Add a third workspace member, `vault-core/`, for code both services need: path normalisation, policy model, advisory lock, atomic write, version hash, history store, secret rules, audit writer, alert sender, review-queue appender, frontmatter parsing. `vault-mcp/` and `vault-watcher/` each depend on it, and each keeps its own Dockerfile and tests as §14 requires.
- **Rejected:** Duplicating the code in each service. A single package with two entrypoints.
- **Why:** The MCP write-time secret check and the watcher's secret scan must use the same rules (§16.13). Both services must agree on the lock, the history layout, and `read.deny`. Duplicated code drifts. The only cost is one extra directory beyond §14.

### D3 — Container conventions
- Spec: §4, §12, §15.
- **Choice:** Kit images are based on `python:3.13-slim`, pinned by digest, built multi-stage with uv. Every kit container runs as `${PUID}:${PGID}`, the same uid/gid Ignis writes as (new env vars, default 1000). One process per container. Operator CLIs run through `docker compose exec|run --rm <service> <cli>`. `vault-mcp` runs a single uvicorn worker.
- **Rejected:** Root containers. A multi-worker server.
- **Why:** If uids differ, Ignis and MCP fight over file ownership in `/vault`. Rate limits and the lease sweeper keep in-process state, which needs a single worker.

### D4 — Ignis mount layout and data placement ⚠
- Spec: §4, §8, §12 backups.
- **Choice:**
  - `VAULT_PATH` → `/vaults/${VAULT_NAME}`. New var `VAULT_NAME`, default `Docs`; it is the vault name shown in Obsidian.
  - Ignis `/app/data` → `${DATA_PATH}/ignis` (bind mount).
  - `/app/obsidian-app` → named volume `obsidian-app`, which is a re-downloadable cache and is not backed up.
  - Set `WS_ORIGINS=https://${IGNIS_HOST}`.
  - Ignis listens on 8080 inside the container, published as `${BIND_ADDR}:${IGNIS_PORT}`.
- **Rejected:** A named Docker volume for Ignis state.
- **Why:** Keeping all state under `DATA_PATH` means backup and restore only touch `/vault` + `/data`, with no reaching into `/var/lib/docker`. `WS_ORIGINS` stops cross-site WebSocket hijacking of a tool that has no auth of its own.

### D5 — Compose structure and ingress selection
- Spec: §4 services, §5.2 `INGRESS=`, §14 `ingress/` ("cloudflare/, caddy/ compose overrides + configs").
- **Choice:**
  - The base `docker-compose.yml` defines `ignis-init`, `ignis`, `vault-mcp`, and `vault-watcher`, plus `git-sync` and `agent-runner` behind compose `profiles:`.
  - Ingress services live in override files as §14 says: `ingress/cloudflare/compose.yml` (cloudflared) and `ingress/caddy/compose.yml` (caddy + oauth2-proxy), each next to its configs. `local` and `none` add no override.
  - `scripts/setup` writes two values into `.env`:
    - `COMPOSE_FILE`, e.g. `docker-compose.yml:ingress/cloudflare/compose.yml`, with `COMPOSE_PATH_SEPARATOR=:`, derived from `INGRESS`
    - `COMPOSE_PROFILES`, from `GIT_REMOTE` and `ANTHROPIC_API_KEY`/`AGENT_COMMAND`
  - After changing `INGRESS`, rerun `scripts/setup --reconfigure`.
- **Rejected:** `include: ingress/${INGRESS}/compose.yml`, which hits the compose interpolation bugs above. Requiring `-f` on every command, which users forget. Folding ingress services into the base file as profiles, which contradicts §14.
- **Why:** It keeps the §14 layout, plain `docker compose up` works, and Compose reads both `COMPOSE_FILE` and `COMPOSE_PROFILES` from `.env` natively.

### D6 — Advisory lock
- Spec: §6.6, §11, §10.2.
- **Choice:**
  - `flock(2)` exclusive lock on `/data/locks/vault.lock`. The holder writes its name (`vault-mcp`, `vault-watcher`, `git-sync`, `backup`, `upgrade-vault`, `restore`) to `/data/locks/vault.owner` after acquiring.
  - Python uses `fcntl.flock` in a worker thread behind an in-process `asyncio.Lock`. Bash uses `flock(1)`.
  - Hold times: MCP holds it for a single tool's read-check-write. git-sync holds it for add/commit/rebase but not push. The watcher holds it only while reading a file for a snapshot.
- **Rejected:** A lockfile inside the vault, which Ignis and git would see. POSIX `fcntl`/`lockf` locks, which do not interoperate with `flock(1)`. Redis.
- **Why:** flock works across containers that share a bind mount on the same kernel, and it is released automatically when a process crashes. The owner file feeds attribution (D21). Ignis cannot take the lock; the window that leaves open is documented in LIMITATIONS.

### D7 — Version hash and atomic write
- Spec: §6.4 `read`/`write`, §16.9.
- **Choice:**
  - `version` is the lowercase hex sha256 of the file's raw bytes. `null` means the file does not exist.
  - Writes happen under the lock: re-hash the file, compare with `base_version`, write to `.<name>.vault-mcp-<rand>.tmp` in the same directory, `fsync`, `os.replace`, and keep the original mode.
  - A mismatch returns `conflict` with `current_version`.
- **Rejected:** mtime- or size-based ETags. Writing in place.
- **Why:** mtime is unreliable across Ignis autosaves and git checkouts. Same-directory rename is atomic, so Ignis never sees a half-written file. Obsidian ignores dotfiles.

### D8 — Path, scope, and deny rules
- Spec: §6.3 `--folders`, §6.5 `read.deny`, §16.7.
- **Choice:**
  - Tool paths are vault-relative POSIX. Reject absolute paths, `..`, NUL, backslash, and any path whose `realpath` escapes `/vault`.
  - **Tightened in T18:** any path that goes through a symlink is refused, even one pointing inside the vault, because policy is decided on the path string (`wiki/x.md -> ../private/y.md` would inherit wiki/'s permissions). The watcher records and indexes no symlinked paths either.
  - Never served or indexed: `.obsidian/`, `.git/`, `.trash/`, any dotfile, and `read.deny` prefixes.
  - Credential `folders` is a list of prefixes with a trailing slash. It limits **read, list, search, and write**. Effective write set = credential folders ∩ policy write rules (D9).
  - Matching is case-sensitive.
- **Rejected:** Applying `--folders` only to writes. Glob patterns.
- **Why:** §16.7 requires the scope on read, search, and write. Prefixes match how the policy file is written and are easy to audit.

### D9 — Write-permission rule (maintainer)
- Spec: §6.5.
- **Choice:** For a target path, read the **current** file's frontmatter `maintainer` (`human|agent|shared`). Allow the write iff `maintainer ∈ {agent, shared}`, or the path is under `write.allow` and `maintainer` ≠ `human`. If the file is new, there is no maintainer and only `write.allow` applies. Otherwise return `forbidden` with a hint to call `review_queue_add`. `delete: false` means no delete tool exists and `move` keeps the source content. `max_file_mb` is checked on the resulting file size.
- **Rejected:** Taking `maintainer` from the incoming content, which would let an agent declare itself maintainer. Treating a missing `maintainer` outside `write.allow` as writable.
- **Why:** Deciding from the file on disk means an agent cannot escalate its rights by rewriting frontmatter.

### D10 — Tool result and error contract
- Spec: §6.4.
- **Choice:** Successful calls return JSON objects (e.g. `read` → `{path, content|content_base64, version, mime}`, and writes → `{path, version}`). Failures raise FastMCP `ToolError` (`isError: true`) whose text is JSON `{"code", "message", "details"}`. Codes: `conflict`, `already_claimed`, `not_claimed`, `forbidden`, `not_found`, `rate_limited`, `secret_detected`, `too_large`, `invalid`, `ambiguous_heading`, `history_pending` (D21; `details.applied` says whether the change landed).
- **Rejected:** Reporting failures as success payloads with a status field.
- **Why:** Clients show `isError` results as failures, so agents do not mistake a rejected write for a success. Machine-readable codes keep e2e assertions stable.

### D11 — Section addressing (`replace_section`, `insert_after`)
- Spec: §6.4.
- **Choice:**
  - Parse with `markdown-it-py` (token line maps; ATX and setext headings; headings inside code fences ignored).
  - `heading` is either the heading text (`Usage`) or the full line (`## Usage`). It must match exactly one heading, otherwise `ambiguous_heading` or `not_found` with the candidate list.
  - A section runs from its heading line to the line before the next heading of the same or higher level.
  - `replace_section` replaces the body and keeps the heading line.
  - `insert_after` inserts directly below the heading line, ahead of the existing body.
- **Rejected:** Regex heading detection, which breaks inside fences. Inserting `insert_after` content at the end of the section.
- **Why:** CommonMark-correct parsing avoids hitting `#` lines inside code. The insert position follows the tool's name; end-of-file appends already have `append`.

### D12 — `move` and wikilink rewriting
- Spec: §6.4 `move` ("rename + update wikilinks"), §6.5 `human_pages: read_only`.
- **Choice:**
  - Resolve links the way Obsidian does: shortest unique basename, otherwise full path.
  - Rewrite `[[x]]`, `[[x|alias]]`, `[[x#h]]`, `![[x]]`, and relative `[t](x.md)`.
  - **All-or-nothing preflight:** before any side effect, collect every file whose links resolve to the source. If any of them is not writable by the credential (D8/D9: a human page, out of folder scope, or `read.deny`), reject the whole move with `forbidden`.
    - `details.blocking_files` lists the paths the credential can see. Files in `read.deny` or outside its scope are reported only as a count, so their paths don't leak.
    - The agent can then call `review_queue_add` to ask a human to do the move.
  - When the preflight passes, the rename and all link rewrites run under one lock hold, as D21 transactions sharing a `group_id`: prepare all, apply all, commit all. Every touched file gets a history version and an audit line.
  - Crash recovery handles each entry on its own. If a group was only partly applied, the watcher alerts and lists the group.
- **Rejected:**
  - Moving anyway and leaving stale links in pages the agent can't write: that breaks the §6.4 contract ("update wikilinks").
  - Allowing link-only edits on `maintainer: human` pages as a special exception: that breaks §6.5.
- **Why:** Both spec rules hold together. A move either fully updates links or doesn't happen, and human pages are never edited by agents.

### D13 — Binary files over MCP
- Spec: §6.4 `read`, §9 `attachments/`, `raw/`.
- **Choice:** `read` returns UTF-8 text as `content`. Other files (PDF, images) come back as `content_base64` + `mime`, up to `max_file_mb`. Write tools accept text only.
- **Rejected:** Refusing to read binaries.
- **Why:** Humans drop PDFs into `raw/` through Ignis, and the ingest agent must be able to read them over MCP.

### D14 — Source lifecycle
- Spec: §6.4 `submit_source`/`claim_source`/`complete_source`/`release_source`, §16.11.
- **Choice:**
  - `submit_source` writes `raw/<YYYY-MM-DD>-<slug(title)>.md` (with `-2`, `-3`… on collision). Frontmatter: `title, origin, url?, submitted_by, submitted_owner, submitted_at, content_sha256`.
  - **`url` is never fetched by the server.** A URL-only submission creates a stub that the ingest agent fetches with its own tools.
  - Leases live in `/data/state.db` (`leases(path, original_path, credential_id, expires_at)`).
  - `claim_source` takes the lock, checks that the file sits directly in `raw/`, renames it into `raw/_processing/`, inserts the lease, and returns the new path. A second caller gets `already_claimed`.
  - Only the lease holder can `complete_source` or `release_source`. `complete_source` requires `summary_page` to exist.
  - A sweeper runs every 60 s and returns expired leases to `raw/`.
- **Rejected:** Server-side fetching, which brings SSRF risk, egress, and content-extraction dependencies. Lease metadata in file frontmatter, which changes the source's hash and leaks into the vault.
- **Why:** The lock + rename gives exactly-one-winner semantics. Keeping lease state outside the vault keeps the vault clean.

### D15 — Review queue format
- Spec: §6.4 `review_queue_add`, §7.3, §16.10.
- **Choice:** Append to `_system/review-queue.md` one Obsidian task per item: `- [ ] <UTC ts> [[page]] — issue — suggestion — by <name> (<owner>) #review ^rq-<id>`. Secret-scan items carry `#review/high`. Humans resolve an item by ticking it; lint archives ticked items.
- **Rejected:** One file per item. A YAML list in frontmatter.
- **Why:** Ticking a checkbox in Obsidian is the simplest action for non-technical users. Block IDs give agents a stable reference.

### D16 — Credential store and bearer tokens
- Spec: §6.3, §16.5.
- **Spec-fixed (not decided here):** one store at `/data/tokens.db`; credentials stored hashed; a name and an owner on every credential; `read|write` scope; optional `--folders` and `--expires`; `token list|revoke` covers OAuth grants too. Everything below is implementation choice within that contract.
- **Choice:**
  - Token = `vmcp_` + 32 random bytes in base64url. Store only `sha256(token)`; the first 8 characters are shown as an id.
  - `tokens.db` tables:
    - `credentials` (id, kind=`bearer|oauth`, name, owner, scope=`read|write`, folders JSON, created_at, expires_at, revoked_at, last_used_at)
    - `token_hashes` (hash, credential_id, type=`bearer|access|refresh`, expires_at)
    - `oauth_clients`
    - `auth_codes`
    - `approval_codes`
  - `write` implies `read`. `--expires` defaults to `90d`; `never` must be given explicitly (setup uses it for the `agent-runner` token).
  - stdio mode authenticates with a `VAULT_MCP_TOKEN` env var.
- **Rejected:** argon2/bcrypt (slow on every request; unnecessary for 256-bit random secrets). Self-contained JWTs, which cannot be revoked without a lookup anyway.
- **Why:** One lookup path covers bearer and OAuth tokens. `token list` shows OAuth grants and `token revoke` kills them (§6.3).

### D17 — OAuth 2.1 authorization server
- Spec: §6.3 (2), §5.1 (4), §16.6.
- **Spec-fixed (not decided here):** OAuth 2.1 per the MCP authorization spec, with protected-resource and authorization-server metadata, DCR, auth code + PKCE, and refresh tokens; consent showing the scope; grants stored in the same `tokens.db`, listed by `token list`, and revocable; upstream-OIDC and admin-approval-code login modes. Any library has to meet this contract, and that requirement is why `OAuthProxy` is rejected below.
- **Choice:**
  - Subclass FastMCP `OAuthProvider`, backed by `tokens.db` (D16). Access tokens are opaque, live 1 h, and are hashed. Refresh tokens live 30 days, rotate on use, and are revoked when reuse is detected.
  - Scopes: `vault:read`, `vault:write`.
  - Endpoints (**changed by T2**):
    - The SDK endpoints stay at FastMCP's defaults on the MCP host root: `/authorize`, `/token`, `/register`, `/revoke`.
    - The kit's own pages live under `/oauth/`: `login`, `consent`, `callback` (FastMCP `custom_route`; T2 confirmed they coexist).
    - Metadata: `/.well-known/oauth-authorization-server` at the root, with `issuer` = `MCP_OAUTH_ISSUER`. Protected-resource metadata is path-aware at `/.well-known/oauth-protected-resource/mcp`. `WWW-Authenticate` on a 401 from `/mcp` carries `resource_metadata` pointing there.
  - **Why not `/oauth/token` etc. (T2):**
    - On the pinned fastmcp 3.4.11, moving the SDK endpoints under `/oauth/` makes the authorization-server metadata report `issuer` = `…/oauth`. The MCP SDK client (1.30.0) then aborts with `Authorization server metadata issuer mismatch`.
    - On fastmcp 4.1.6 the issuer can be kept at the root. But the SDK client's refresh falls back to `<issuer>/token` when it has stored tokens and no cached metadata, and that path returned `404` and forced a full re-authorization.
    - With root paths, both versions ran the whole flow, including a refresh that needed exactly one extra `POST /token`. Ingress already routes the whole MCP host without login (D39, §5.1 item 4), so root paths need no extra ingress rule.
  - **No `required_scopes` on the provider (T2):** on fastmcp 4.x, setting it puts `scope="vault:read"` into `WWW-Authenticate`, and the SDK client then requests only `vault:read`. On 3.4.11 the client requested `vault:read vault:write` either way. Leaving it unset keeps write grants requestable on both. The consent page decides what is granted, and vault-mcp enforces read/write per tool against the credential's scope.
  - **To check in T24:** FastMCP advertises `token_endpoint_auth_methods_supported: [client_secret_post, client_secret_basic]` with no `none`. Check that public PKCE-only clients (e.g. Claude.ai) still register; add `none` if not.
  - The consent page is server-rendered HTML with no JS. It shows client name, redirect host, and the requested scope.
  - Each grant becomes a `credentials` row named `oauth:<client_name>`, owned by the user's email.
  - Approval codes: `vault-mcp oauth approve-code --owner <email> [--scope] [--ttl 1h]` prints a one-time 10-character base32 code. It is stored hashed and is single-use.
- **Rejected:** FastMCP `OAuthProxy`. It keeps clients and tokens in its own encrypted KV, outside `token list`; it passes upstream tokens through; and it has no approval-code mode.
- **Why:** It is the only option where OAuth grants and bearer tokens share one store, one revoke path, and one scope model, as §6.3 requires.

### D18 — Upstream identity for OAuth login
- Spec: §6.3 (2a), §13 `OAUTH_UPSTREAM_*`.
- **Choice:**
  - `authlib` OIDC client. `google` and `microsoft` use OIDC discovery. `oidc` uses `OAUTH_UPSTREAM_ISSUER`, a new env var. `github` uses the OAuth app + `/user/emails` and accepts only primary, verified emails.
  - The email domain must be in `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`.
  - `none` → approval-code form.
  - Upstream tokens are thrown away after login.
- **Rejected:** Keeping upstream tokens. Per-provider SDKs.
- **Why:** Login only needs a verified email, so storing nothing upstream shrinks the blast radius. GitHub is not OIDC, so it needs the email API path.

### D19 — Rate limiting
- Spec: §6.5 `rate_limits`, §16.8.
- **Choice:** In-memory sliding-window counters per credential id, in FastMCP middleware on tool calls. Writes count toward both the request limit and the write limits. Over the limit → `rate_limited` with `retry_after_s`. If one credential hits the limit ≥ 5 times in 10 min, one alert is sent per credential per hour. Counters reset when the process restarts.
- **Rejected:** SQLite-persisted counters (a disk write on every request). Proxy-level limiting, which cannot see credentials.
- **Why:** There is a single worker (D3), so memory is authoritative. Losing counters on restart only matters to someone who can restart the container.

### D20 — Audit log
- Spec: §6.6, §16.15.
- **Choice:**
  - `/data/audit.log` is JSON Lines. Fields: `ts` (RFC 3339 UTC, ms), `id` (ULID), `service`, `credential_id`, `credential_name`, `owner`, `tool`, `path`, `new_path?`, `old_version`, `new_version`, `result` (`ok` or error code), `detail`.
  - **Every tool call** is logged, reads included.
  - Each line is written with one `write()` on an `O_APPEND` fd. The watcher appends `secret_detected` lines the same way.
  - No rotation in v1; documented at about 0.3 KB/call. The disk > 80 % alert covers growth.
- **Rejected:** Logging writes only. Logrotate inside the containers.
- **Why:** Read logs are needed for offboarding and exfiltration investigations (SECURITY.md). Single-write appends are safe across two processes on a local filesystem.

### D21 — History store and durable MCP handoff
- Spec: §6.6 ("history handled by vault-watcher for all writers"), §7.1, §16.14–15.
- **Choice:**
  - **`vault-watcher` is the only writer of history pointers and blobs.** vault-mcp writes only to a spool directory, `/data/history/.inbox/`, and reads history heads.
  - **Store:**
    - Blobs go to `/data/history/.objects/<sha[:2]>/<sha>`, gzip, content-addressed.
    - Each version is a pointer file `/data/history/<vault-path>/<YYYYMMDDTHHMMSS.ffffffZ>.json` = `{sha256, event: create|modify|rename|delete, from_path?, source: mcp|web|git|unknown, audit_id?}`. A delete writes a tombstone pointer.
    - The **new** content is recorded on every change, plus a baseline at the first scan, so the "previous version" always exists.
  - **Durable per-write handoff with an explicit commit point.** Every MCP tool that changes a file does this while holding the lock:
    1. **Liveness pre-flight:** if the watcher heartbeat (`/data/vault-watcher/heartbeat`) is older than 30 s, return `history_pending` *before changing anything*.
    2. **Prepare (T3 ordering):**
       - First write and fsync everything that doesn't depend on the current file: `post.blob` (the exact new bytes), the same-directory temp file for the replace, and `txn.json` (`{audit_id, path, new_path?, post_sha256, credential, tool}`). Entries in `.prepared` state are never consumed by the normal drain.
       - Then read the current file. If its sha256 differs from the newest pointer (an un-recorded Ignis edit still inside the debounce window), write `pre.blob` with those bytes. Always write `pre.json` (`{pre_sha256, event}`). Fsync both, plus the directory. **Every byte recovery needs is durable before step 3.**
    3. **Revalidate and apply:** re-read the file and compare it with the bytes from step 2, immediately before `os.replace`. If they differ, Ignis saved in between:
       - `write`/`replace_section`/`move` (which carry a `base_version`) return `conflict` and drop the prepared entry;
       - `append`/`insert_after` redo step 2 against the new content, **at most 3 times**. If the file still keeps changing, they return `conflict` with nothing applied and the prepared entry dropped, so a busy editor can't hold the lock indefinitely.
       Then fsync the directory. Recovery also deletes a leftover `.<name>.vault-mcp-<id8>.tmp` file.
    4. **Audit:** append the audit line, carrying `audit_id`, and **fsync it** before the commit rename. Otherwise a power loss could leave a committed history version with no audit line, which the drain of committed entries does not repair.
    5. **Commit:** rename `<audit_id>.prepared` → `<audit_id>.committed` and fsync `.inbox/`. This rename is the commit point.
    6. Release the lock and **wait for the acknowledgement**: the watcher deletes the committed entry once its pointers are fsynced. The wait is bounded by `HISTORY_ACK_TIMEOUT` (new env var, default 5 s; T3 measured p50 6–12 ms and p95 10–15 ms for back-to-back writes, so 5 s leaves room for a drain that's waiting on the lock).
    - Success is returned only after the acknowledgement. On ack timeout the tool returns `history_pending` with `details: {applied: true, version}`. The committed entry is durable, so the version is recorded when the watcher resumes.
  - **Watcher consumption is idempotent.** Each drain takes the lock and runs at startup, on inotify events in `.inbox/`, and before handling any vault fs event. For every entry it first **merges `pre.json` into `txn.json`**: `pre_sha256` and `event` live there, so classification and pointer creation need both. It then consumes `.committed` entries and runs crash recovery on `.prepared` ones (below). For each entry it records `pre` (as `source: web`, from `pre.blob`) when present, then `post` (as `source: mcp`, with `audit_id`). A pointer that already carries `audit_id` + kind is skipped. Pointer timestamps come from `txn.json`, so order is preserved.
    - The watcher takes the lock before it hashes a file for a vault event (D6). An MCP write's fs event is therefore handled only after the commit, when its version is already recorded (hash = head → skip).
    - An Ignis edit that lands after the replace is an ordinary later `web` version. It can no longer erase the MCP version.
  - **Crash recovery for prepared entries.** vault-mcp releases the lock with `.prepared` entries outstanding only when it has died, or (T19) when it abandons a part-applied move group after a file changed mid-group. Either way, any `.prepared` entry the watcher sees **while it holds the lock** is an orphan to classify. The watcher classifies it from the vault file's current hash:
    - **= `post_sha256`** → the write was applied. Record the pointers. If `audit.log` has no line for `audit_id` (crash between steps 3 and 4), append one with `result: ok, recovered: true`. Then delete the entry.
    - **= `pre_sha256`** (or the file is still absent for a create) → never applied. Record nothing except `pre` if it was spooled, append an audit line `result: aborted`, and delete the entry. No phantom version.
    - **Neither** (Ignis saved again between the crash and recovery) → the outcome can't be determined. Record `post` with pointer flag `unconfirmed: true` so the data is kept and labelled honestly, append audit `result: unknown`, send an alert, and delete the entry.
    - Recovery runs at watcher startup and on every drain. The drain always holds the lock, so any `.prepared` entry it finds is an orphan by construction, with no age heuristic.
  - **Non-MCP changes:** debounced (5 s quiet, capped at 60 s; Ignis autosave bursts). Attribution: lock owner `git-sync` → `git`. Anything else → `web`, documented as also covering shell edits by the PUID user. `unknown` is used for full-scan discoveries.
  - A daily GC removes pointers older than `HISTORY_RETENTION_DAYS` except the newest `HISTORY_MIN_VERSIONS`, then sweeps unreferenced blobs. The spool is never GC'd.
- **Rejected:**
  - vault-mcp writing pointers and blobs itself: it contradicts §6.6 and splits ownership.
  - A pre-write head check alone: a watcher crash after `os.replace`, or an Ignis save before the event is handled, loses the MCP `new_version`.
  - A single-phase spool (entry visible before `os.replace`): a vault-mcp crash before the replace records a phantom version, and a crash before the audit append leaves a version with no audit line.
  - Tailing `audit.log` to match hashes: the same race, because the bytes are gone once the file changes again.
  - No debounce for web edits: Ignis autosave produces a version per keystroke burst.
  - Hardlink snapshots: nowhere to keep metadata.
- **Why:** The exact bytes are on disk before the vault file changes, and success waits for the watcher's acknowledgement. Every reported MCP write therefore has a version (§16.15), crashes cannot lose one, and the watcher stays the single history owner (§6.6/§7.1).
- **T3 results** (prototype: watcher in a container on bind mounts, writer on the host; throwaway, deleted):
  - 20/20 back-to-back MCP writes interleaved with Ignis-style saves had a post pointer and an audit line.
  - Watcher stopped for more than 30 s → `history_pending`, file unchanged, nothing spooled.
  - Watcher frozen mid-ack → `history_pending` with `applied: true`, and the version was recorded after restart.
  - vault-mcp killed: (a) before replace → no version, audit `aborted`, temp file removed; (b) after replace → version + one `recovered` audit line; (c) after audit → version, no duplicate audit line. Killed after replace with a pre-image pending → both the un-recorded web version and the MCP version were recorded.
  - An Ignis save between the kill and recovery → `unconfirmed` pointer that keeps the MCP bytes, audit `unknown`, and an alert. In the prototype, the web content that was on disk at recovery got **no** pointer: the watcher was stopped when it was saved, and the prototype's startup baseline skipped files that already had a head. The real watcher's startup scan (D22) must record any file whose hash differs from its head, as `source: unknown`; T13's acceptance now checks this.
  - inotify events on the bind mount reached the containerised watcher (pointer after 5.1 s = 5 s debounce + latency).
  - **The ordering matters.** With the pre-image fsyncs between read and replace, the unlocked-Ignis window was ~16 ms, and 2 of 18 web saves were silently overwritten in 2 of 3 runs. With the step-2/3 ordering above the window is 0.04–0.1 ms, and 0 of 18 were lost in 3 runs. **The race is narrowed, not closed:** Ignis takes no lock, so a save that lands between the final compare and `os.replace` is still overwritten. Three clean runs don't show the window is gone. This residual race goes into LIMITATIONS (T41).
  - Debounce defaults (5 s quiet / 60 s cap) and the 30 s heartbeat staleness are kept.

### D22 — Watcher change detection
- Spec: §7.
- **Choice:** watchdog `InotifyObserver` with a 10-minute full scan as fallback. The scan compares `(mtime, size)` and hashes only on difference. Ignored: `.obsidian/workspace*.json`, `.obsidian/cache`, `.trash/` (tombstones only), `.git/`, `*.tmp`, `.*.vault-mcp-*`. The watcher logs a startup error naming the sysctl fix if inotify watch limits are hit.
- **Rejected:** Polling only.
- **Why:** inotify gives low latency for history and index. The scan catches events missed during restarts and bursts such as git pull.

### D23 — Search index
- Spec: §7.2, §6.4 `search`.
- **Choice:**
  - `/data/index.db`, WAL mode. A `docs(path PK, title, headings, body, frontmatter, sha256, mtime)` table plus an external-content FTS5 table (tokenizer `unicode61 remove_diacritics 2`, `prefix='2 3'`).
  - Owned and written by the watcher. `vault-mcp` opens it read-only (`?mode=ro`).
  - `read.deny` paths and non-Markdown files are **never indexed**.
  - Queries: each user term is quoted, so FTS syntax cannot be injected or error out. bm25 weights: title 10, headings 5, path 3, frontmatter 2, body 1. Returns `{path, title, snippet, score}`. Folder scope is applied in SQL. `limit` defaults to 20, max 100.
- **Rejected:** Separate MCP-facing and full indexes; nothing else reads it. The Porter stemmer, which is English-only on what may be a multilingual vault. The trigram tokenizer, which roughly triples index size.
- **Why:** Simplest arrangement that satisfies §7.2 and enforces deny/scope rules at the data layer.

### D24 — Secret detection
- Spec: §6.5 `secrets`, §7.3, §16.13.
- **Choice:**
  - A curated regex rule set in `vault-core/secrets.py`, ported from gitleaks' high-confidence rules (MIT): AWS, GCP, Azure, GitHub, GitLab, Slack, Stripe, Anthropic, OpenAI, private-key blocks, JWTs, connection strings with passwords, generic `password=`/`api_key=` assignments.
  - No entropy rules. Fixtures for every rule.
  - **MCP:** scan only lines added or changed against the current file, so existing human content cannot block appends.
  - **Watcher:** scan changed files. Findings are deduped by `(path, rule, sha256(match))` in `index.db`.
  - Matches are masked everywhere: review queue, alerts, audit show rule id + line only.
- **Rejected:** detect-secrets (entropy noise on hashes and `version` strings; heavier plugin API). The gitleaks binary (Go RE2 rules, a subprocess per write, and a binary in two images). Any in-content allowlist marker: agents could write it themselves and get around the `reject_on_write: true` that §6.5 requires.
- **Why:** The rules are deterministic and testable, need no extra dependency, and are identical in both services (D2). Masking stops the review-queue entry from re-triggering the scan. False positives are handled by narrowing the rule, with a fixture test, not by a bypass.

### D25 — Alert delivery
- Spec: §12 alerts.
- **Choice:**
  - `vault-core/alerts.py` and `scripts/lib/alert.sh` (curl) pick the payload from `ALERT_WEBHOOK`'s host: `hooks.slack.com` → `{"text"}`, `discord.com/api/webhooks` → `{"content"}`. Anything else gets the generic `{"event","severity","service","message","details","ts"}`.
  - Dedupe key per event type + subject, 1 h window, kept in memory per process.
  - Delivery failure is logged and never blocks the operation.
- **Rejected:** An Apprise/notification library.
- **Why:** Three payload shapes cover what §12 names, at roughly 40 lines per language.

### D26 — Operational monitoring
- Spec: §12 (container unhealthy, disk > 80 %).
- **Choice:**
  - Each service has a compose `healthcheck` (`/health` for vault-mcp and the watcher's local HTTP, a heartbeat file for git-sync and agent-runner).
  - The `vault-watcher` monitor loop checks every 60 s: peers' `/health` over the compose network, heartbeat file age, and disk usage of `/vault` and `/data`. It alerts after 3 consecutive failures.
- **Rejected:** Mounting the Docker socket to watch container health, which is root-equivalent.
- **Why:** This covers §12 without handing any container control of the host.

### D27 — Policy and config files
- Spec: §6.5, §13.
- **Choice:** Ship `vault-mcp.example.yaml` with the §6.5 defaults. `scripts/setup` copies it to `vault-mcp.yaml`, which is gitignored like `.env`, and the file is mounted read-only at `/config/vault-mcp.yaml` into vault-mcp and vault-watcher. Both validate it with pydantic and refuse to start on errors. Changes need a restart. Every service logs a startup warning if `BIND_ADDR=0.0.0.0` (§5.4).
- **Rejected:** Committing a live `vault-mcp.yaml`, which conflicts on every kit upgrade. Hot reload.
- **Why:** This is the same pattern as `.env`, and failing fast surfaces bad policy before any request is served.

### D28 — Ignis plugin allowlist and managed settings
- Spec: §8, §16.17.
- **Choice:**
  - `ignis/ignis-plugins.yaml` entries look like `{id, repo, version, sha256: {main.js, manifest.json, styles.css?}, writable_data: bool}`.
  - A one-shot `ignis-init` service (kit Python image) runs `ignis/harden.py` before Ignis starts (`depends_on: service_completed_successfully`). It downloads GitHub release assets into `${DATA_PATH}/ignis-plugins/`, verifies the sha256 values, writes `.obsidian/community-plugins.json`, removes non-allowlisted plugin dirs, and resets managed `.obsidian/*.json` files from `vault-template/.obsidian/`.
  - Ignis gets `${DATA_PATH}/ignis-plugins` bind-mounted **read-only** over `/vaults/${VAULT_NAME}/.obsidian/plugins`. For plugins with `writable_data: true`, `data.json` is bind-mounted read-write as a single file.
  - `ignis-init` creates `.obsidian/plugins/` and every single-file `data.json` mount target in the vault, owned by `PUID:PGID`, before Ignis starts. *(T1: Docker creates any missing bind-mount target as `root`, and Ignis can't use a root-owned plugins directory.)*
- **T1 result:** the read-only mount works. Ignis starts, allowlisted plugins load, `saveData()` works through the single-file `data.json` mount, and installing from the UI fails with `ENOENT` (a control run with a writable directory installed successfully). No fallback is needed.
- **T1 limit:** Obsidian keeps "restricted mode" in each browser's localStorage (`enable-plugin-<vault>`), so allowlisted plugins run only in browsers where the user has switched community plugins on. The kit can't set this on the server (see impl-notes open question).
- **Rejected:** Vendoring plugin code into the template (third-party licences, stale code). Making `.obsidian/` entirely read-only, which breaks workspace state.
- **Why:** Plugins are checksum-pinned and installs from the UI fail, which meets §16.17. Settings files that should change at runtime stay writable and are reverted at the next start.

### D29 — Obsidian version pinning
- Spec: §8, §13, §16.18.
- **Choice:** `.env.example` pins `IGNIS_IMAGE` by digest and sets `OBSIDIAN_VERSION` to **exactly** the version that digest pins. `docs/UPGRADING.md` changes both together. Ignis does not guarantee newer versions. **T1 finding:** an *older* value doesn't fail startup; Ignis logs `OBSIDIAN_VERSION=<v> is older than this build's Obsidian <pin>; using <pin>.` and runs its pin. So the e2e check (§16.18) must assert that the startup log line `Obsidian (ready|already set up) (v<X>)` has X = `OBSIDIAN_VERSION`, not merely that the container started. Pin found in T1: digest `sha256:2049be7d…bb5` → Obsidian `1.14.4`.
- **Rejected:** Leaving `OBSIDIAN_VERSION` unset.
- **Why:** An explicit value makes §16.18 testable and makes upgrades visible in diffs.

### D30 — agent-runner
- Spec: §10.2, §16.19–20.
- **Choice:**
  - Image: `node` LTS slim + pinned `@anthropic-ai/claude-code` + `supercronic`, with the kit skills baked in as a fallback.
  - It connects to `http://vault-mcp:8082/mcp` with the `agent-runner` bearer token. A generated `.mcp.json` carries the header. The vault's `AGENTS.md` and `skills/` are mounted **read-only**; there is no write mount.
  - Jobs run `claude -p` with `--output-format json` and do not overlap (`flock -n` on `/data/agent-runner/job.lock`).
  - Spend is read from the result's `total_cost_usd` and accumulated per UTC month in `/data/agent-runner/spend.json`. When spend ≥ `AGENT_MONTHLY_BUDGET` (USD), the job is skipped and an alert sent.
  - Custom `AGENT_COMMAND` contract: env `VAULT_MCP_URL`, `VAULT_MCP_TOKEN`, `JOB=ingest|lint`. If it prints a JSON line with `total_cost_usd`, budget enforcement works; otherwise a startup warning says the budget is unenforced.
- **Rejected:** Direct filesystem writes from the runner, which bypass policy, audit, conflicts, and history. Host cron. Ofelia, which needs the docker socket.
- **Why:** Server agents follow the same rules as everyone else (§16.15). supercronic is built for containers and logs to stdout.

### D31 — Agent commit messages through git-sync
- Spec: §10.2 commit format, §11.
- **Choice:** After a job, agent-runner writes `/data/git-sync/requests/<ts>.json` `{type: ingest|lint, summary}`. git-sync waits with `inotifywait -t $SYNC_INTERVAL`, so it wakes either on a request or on the interval, then commits with `docs(<type>): <summary>` and trailer `Agent: agent-runner`. Without a request the commit message is `docs(auto): <ts>`.
- **Rejected:** Giving agent-runner git and a vault write mount.
- **Why:** git-sync stays the only process that runs git (no index-lock races), and agent commits land right after the job.

### D32 — git-sync implementation
- Spec: §11, §16.21.
- **Choice:**
  - Alpine + bash + git + git-lfs + flock + inotify-tools + curl.
  - Auth: SSH deploy key at `${DATA_PATH}/git-sync/ssh/id_ed25519` (+ `known_hosts`), or HTTPS credentials embedded in `GIT_REMOTE`.
  - Cycle: lock → `add -A` → commit if dirty → `fetch` → `rebase`, unlock, then `push`.
  - **On conflict:** `rebase --abort`; copy each conflicting local file to `<file>.conflict-<ts>.md`; `rebase -X ours origin/<branch>`, which keeps the remote side of conflicting hunks and the local non-conflicting changes; commit the conflict copies; push; alert. If modify/delete conflicts or anything else is unresolvable, alert and stop pushing until an admin acts.
  - Setup adopts an existing non-git vault with `init` + initial commit + push, and refuses if the remote is non-empty unless `--adopt-remote` is given.
  - Writes `/data/git-sync/heartbeat`.
- **Rejected:** A merge-based pull. The Kubernetes git-sync image, which only pulls.
- **Why:** This follows the §11 rebase flow, never silently drops either side, and keeps the logic in a script anyone can audit.

### D33 — Operator scripts
- Spec: §14 `scripts/`.
- **Choice:** `scripts/*` are bash (`set -euo pipefail`, checked with shellcheck in CI). On the host they need only docker (compose v2), bash, curl, and openssl. Logic in Python runs inside containers: `token` → `vault-mcp` image; `history`, `reindex`, `upgrade-vault` → `vault-watcher` image.
- **Rejected:** Host-side Python scripts.
- **Why:** Hosts need no Python environment, and the scripts use the same code and versions as the running services.

### D34 — Setup flow
- Spec: §1, §5.4, §9, §16.1.
- **Choice:** `scripts/setup` asks for the values it needs, with defaults, or runs with `--non-interactive` from an existing `.env` (used by CI). Steps:
  1. Writes `.env` (including `COMPOSE_FILE` and `COMPOSE_PROFILES`, D5) and `vault-mcp.yaml`.
  2. Creates `VAULT_PATH` and `DATA_PATH` with PUID ownership.
  3. Copies `vault-template/` if the vault is empty, or offers adoption (D36) otherwise.
  4. Generates the `agent-runner` token when that profile is on.
  5. Runs `docker compose up -d` and waits for health.
  6. Runs `check-exposure` with a temporary read token, then revokes it.
- **Rejected:** A web-based installer.
- **Why:** A single command supports the < 30 min target, and CI can run the same path non-interactively.

### D35 — check-exposure
- Spec: §5.4, §16.4.
- **Choice:**
  - bash + curl + openssl. Each §5.4 check prints PASS/FAIL/SKIP; the exit code is the number of FAILs.
  - The token comes from `CHECK_TOKEN` and is never passed as an argument.
  - "Ignis without credentials is not the app" means the response contains neither of Ignis's markers, `<meta name="apple-mobile-web-app-title" content="Ignis"/>` and `id="ignis-status"` (pinned in T1; the `<title>` is just `Obsidian`, which is too generic).
  - The raw-port probe connects to the host's public IPs on `IGNIS_PORT`/`MCP_PORT` and reports SKIP when no public IP exists.
- **Rejected:** Python or a third-party scanner.
- **Why:** It works on any host and in CI, and keeps the token out of process listings.

### D36 — Template versioning and upgrade
- Spec: §9, §9.1, §16.22.
- **Choice:**
  - `schema_version` is an integer in `AGENTS.md` frontmatter.
  - `vault-template/.kit-manifest.json` lists managed files with their sha256 for each released schema version. A file whose hash differs from the shipped one counts as customised: it gets a 3-way diff and is never overwritten silently.
  - Preserved blocks are `<!-- custom -->…<!-- /custom -->` and are re-anchored under the same heading in the new file.
  - `.vault-overrides` uses gitignore syntax.
  - Writes go through the lock and the history module.
- **Rejected:** Semver schema versions. Single-line `<!-- custom -->` markers with no end marker.
- **Why:** Paired markers have clear bounds, and the manifest separates kit content from deployer edits without git.

### D37 — Backup and restore
- Spec: §12, §16.16.
- **Choice:**
  - `scripts/backup` writes `vault-backup-<UTC>.tar.gz` with no downtime. It takes the lock, copies `*.db` with the SQLite online backup API, tars `/vault` + `/data` (which includes Ignis state per D4), and writes `MANIFEST.json` (kit version, schema_version, sha256 values). `.env` and `vault-mcp.yaml` are included only with `--include-config`, since they hold secrets.
  - `scripts/restore` requires the stack to be stopped, verifies the manifest, extracts, then runs `reindex`.
- **Rejected:** Stopping the stack to back up. Raw-copying live SQLite files.
- **Why:** The backup API gives consistent databases while the system stays available. Restore is rare, so requiring downtime there keeps it simple.

### D38 — Testing, CI, release
- Spec: §15, §16.
- **Choice:**
  - Unit tests per package with pytest.
  - **e2e:** a pytest suite in `e2e/` drives the real compose stack (`local` and `none` profiles) using `fastmcp.Client` + `httpx`. It is added in Phase 1, and each task adds the §16 tests it satisfies. The "unreachable from another machine" check probes the host IP from a container on a separate network.
  - **Nightly:** Playwright (Python) against pinned Ignis for §16.12, §16.17, and §16.18.
  - GitHub Actions: `lint` (ruff, mypy, shellcheck, hadolint), `unit`, `e2e`, `nightly`, `release`. The release workflow runs on a `v*` tag and builds with buildx + QEMU for amd64/arm64 to GHCR.
  - Dependabot covers uv, docker, github-actions, and npm.
- **Rejected:** Renovate, which needs an app install. Mock-only integration tests.
- **Why:** The e2e suite is the executable form of §16. Dependabot is native and needs no setup.

### D39 — Ingress profile specifics
- Spec: §5.2, §16.3.
- **Choice:**
  - `cloudflare`: a remotely managed tunnel (`CF_TUNNEL_TOKEN`). Hostnames are configured in the dashboard and point at the compose service names (`http://ignis:8080`, `http://vault-mcp:8082`). `docs/ingress/cloudflare.md` has click-by-click steps for the Access app (allow rule) and a Bypass app for `MCP_HOST`.
  - `caddy`: a Caddyfile with `forward_auth` to `oauth2-proxy` for `IGNIS_HOST`, and a plain `reverse_proxy` to vault-mcp for `MCP_HOST`. The oauth2-proxy cookie secret is generated by setup.
- **Rejected:** Driving the Cloudflare API from setup (needs an account-scoped API token).
- **Why:** This matches §5.2 "instructions for Access", and the tunnel token is the only Cloudflare secret the kit handles.

## Task List

The ordered checklist is in `tasks/todo.md`, with decision tags and watch/delegate marks. Phase index:

1. Phase 0 — Risk spikes (T1–T3)
2. Phase 1 — Foundation + MCP read slice (T4–T11) → Checkpoint A
3. Phase 2 — Watcher: history, index, search (T12–T15) → Checkpoint B
4. Phase 3 — MCP write path, secret scan, ops monitor (T16–T23) → Checkpoint C
5. Phase 4 — OAuth (T24–T25) → Checkpoint D
6. Phase 5 — Template, skills, Ignis hardening, setup (T26–T29) → Checkpoint E
7. Phase 6 — Ingress (T30–T33) → Checkpoint F
8. Phase 7 — Optional services and lifecycle (T34–T37) → Checkpoint G
9. Phase 8 — Release and docs (T38–T41) → Checkpoint H (all of §16)

The watcher comes before the MCP write path because the D21 handoff needs a running consumer.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| An agent write while a person is typing in the same note drops their unsaved keystrokes (§16.12) | Med | **T1:** an idle open editor shows the new content within ~0.5 s and doesn't overwrite it, so §16.12 passes. Keystrokes typed but not yet saved when the write lands are lost. Documented in LIMITATIONS. Whether to add a guard against writing to notes with recent web edits is an open question in impl-notes. |
| Two people typing in the same note at once: the last save wins silently | Med | **T1:** confirmed. It's an Ignis/Obsidian behaviour the kit can't change; documented in LIMITATIONS. |
| ~~A read-only plugins mount makes Ignis/Obsidian error at startup~~ | — | **T1: resolved.** The read-only mount works; see D28. |
| FastMCP `OAuthProvider` route paths can't be moved under `/oauth/*`, or Claude.ai rejects the metadata | High | **T2 (fastmcp 3.4.11, cross-checked on 4.1.6):** under `/oauth/*` the SDK client fails (issuer mismatch on 3.x, broken refresh fallback on 4.x), so SDK endpoints stay at the root (D17). The full flow passed with the MCP SDK's own `OAuthClientProvider`: discovery from 401 → protected-resource metadata → authorization-server metadata → DCR → authorize with PKCE S256 and `resource` → token → tool call → refresh. **A real Claude.ai connector is still untested** (user chose scripted client only); T2 stays open until the Checkpoint D manual run. |
| The window between Ignis writes and MCP compare-and-replace (Ignis takes no lock) | Med | **T3:** read → durable pre-image → re-read/compare → `os.replace`. The window measured 0.04–0.1 ms, down from ~16 ms when the fsyncs sat inside it. A save that lands in that window is still overwritten; documented as a residual race in LIMITATIONS (T41). |
| Secret-regex false positives block legitimate agent writes | Med | Diff-only scan; high-confidence rules only; each reported false positive becomes a rule fix + fixture (no content-level bypass, D24) |
| ~~The D21 ack wait slows back-to-back agent edits, or inotify misses events on the bind mount~~ | — | **T3: resolved.** Ack p50 6–12 ms / p95 10–15 ms, and inotify works on the bind mount. |
| Claude Code JSON output changes and `total_cost_usd` disappears | Med | Pin the CLI version; a missing field disables jobs with an alert (fail closed on budget) |
| Spec deviations ⚠ (D2, D4) rejected in review | Low | Raised as Open Questions before Phase 1 |

## Open Questions

- D2: is adding `vault-core/` to the §14 layout acceptable?
- D4/D37: Ignis state lives in `${DATA_PATH}/ignis` instead of a Docker volume, so §12 "Ignis data volume" becomes part of `/data`. OK?
- D14: `submit_source(url=…)` creates a stub and never fetches. OK?
- New env vars: `VAULT_NAME`, `PUID`, `PGID`, `COMPOSE_FILE`, `COMPOSE_PATH_SEPARATOR`, `COMPOSE_PROFILES`, `OAUTH_UPSTREAM_ISSUER`, `HISTORY_ACK_TIMEOUT`. They need to be added to §13.
- D21 fails closed: when vault-watcher is down, MCP writes return `history_pending` and change nothing. Is losing write availability acceptable in exchange for the §16.15 guarantee?
