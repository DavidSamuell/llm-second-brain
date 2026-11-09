# Docs Vault Kit — Specification

**Status:** Draft v0.4 · **Date:** 2026-11-09

## 1. Goal

An open-source, self-hostable kit that lets any team (inside or outside BookBot) run a shared Obsidian documentation vault where:

- **Humans** edit in the browser (real Obsidian via Ignis), behind a login of the deployer's choosing.
- **AI agents** — scheduled server-side agents and team members' own agents (Claude Code, [Claude.ai](http://Claude.ai), ChatGPT, Cursor, Codex…) — read and write over MCP, following the LLM Wiki pattern (ingest / query / lint).
- **Every change is versioned and recoverable**, with or without git. Git backup is optional.

The kit contains **no documentation content** — only what is needed to deploy, secure, and operate a vault. A deployer brings (or creates) the vault, picks how to publish it, edits `.env`, and runs `docker compose up`.

## 2. Non-goals (v1)

- Real-time sync for desktop Obsidian clients (possible v2: Self-hosted LiveSync + filesystem bridge).
- Per-user permissions or per-user attribution inside the web editor (Ignis has neither; documented limitation).
- Multiple vaults per deployment (run one deployment per vault for isolation).
- Hosting content or offering a SaaS.
- Bundling or modifying Ignis/Obsidian code (pulled as images/runtime downloads only).

## 3. Users and scenarios


| Actor                        | Does                                                                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Deployer / admin             | Installs the kit on their own Linux server, chooses ingress, manages plugin allowlist, issues/revokes agent access, optionally connects git. |
| Team member (human)          | Logs in via browser, reads/edits docs in Obsidian (Ignis), drops sources into `raw/`, resolves review-queue items.                           |
| Team member's agent          | Connects over MCP (bearer token or OAuth); searches, reads, writes, submits sources fetched from anywhere.                                   |
| Server-side agent (optional) | Runs scheduled ingest and lint against the vault.                                                                                            |


## 4. Architecture

```
                 ┌──────────── deployer's ingress (bring your own or ready-made profile) ───────────┐
 browsers ──────►│  docs.<domain>  (login required)            docs-mcp.<domain>  (no browser login) │◄────── agents
                 └───────────────┬─────────────────────────────────────────┬────────────────────────┘
                                 ▼                                         ▼
                        ignis (127.0.0.1:8081)                  vault-mcp (127.0.0.1:8082)
                                 │                                         │
                                 └──────────────► /vault (host dir) ◄──────┘
                                                     ▲        ▲        ▲
                                  vault-watcher ─────┘        │        └── agent-runner (opt)
                                  (history, index,      git-sync (opt)
                                   secret scan)
                                         │
                                         ▼
                                   /data (outside vault): history/, audit.log, index.db, tokens.db

```

### Services (docker compose)


| Service                  | Purpose                                                                                                        | Required         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- | ---------------- |
| `ignis`                  | Obsidian web editor over `/vault`                                                                              | yes              |
| `vault-mcp`              | MCP gateway over `/vault` — **built by this project** (§6)                                                     | yes              |
| `vault-watcher`          | Watches `/vault`: version history for every change, search index, secret scan — **built by this project** (§7) | yes              |
| `git-sync`               | Periodic commit / pull --rebase / push to a deployer-provided remote                                           | optional profile |
| `agent-runner`           | Scheduled server-side agent jobs (ingest, lint)                                                                | optional profile |
| `cloudflared`            | Ingress profile `cloudflare`                                                                                   | optional profile |
| `caddy` + `oauth2-proxy` | Ingress profile `caddy`                                                                                        | optional profile |


- `/vault` (`VAULT_PATH`): host directory — an existing vault or a new one initialised from `vault-template/`.
- `/data` (`DATA_PATH`): host directory **outside the vault** for history, audit log, search index, and token/OAuth store. Not editable through Ignis; not committed by git-sync.

## 5. Ingress (bring your own, with ready-made profiles)

The kit publishes nothing to the internet by itself. Services bind to `127.0.0.1` by default (`BIND_ADDR`).

### 5.1 Ingress contract

Any proxy, tunnel, or VPN placed in front MUST:

1. Terminate **HTTPS** (Ignis requires a secure context).
2. Support **WebSockets** on the Ignis host.
3. Require **user authentication** before reaching Ignis (SSO/OIDC, email OTP, VPN membership…).
4. Route the **MCP host without interactive login**, including `/mcp`, `/.well-known/oauth-*`, `/oauth/*` — vault-mcp enforces its own auth.
5. Never expose `IGNIS_PORT` / `MCP_PORT` directly.

### 5.2 Ready-made profiles (`INGRESS=` in `.env`)


| Profile                | What it sets up                                                                                                                                        | Cost                | Needs                                                      | Best for                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- | ---------------------------------------------------------- | ------------------------------------------------ |
| `cloudflare` (default) | `cloudflared` Tunnel + instructions for Cloudflare Access: Ignis app (email/domain/IdP rule); MCP host set to **bypass** (vault-mcp handles auth)      | Free up to 50 users | Domain with DNS on Cloudflare. No public IP or open ports. | Most teams; servers behind NAT/firewall          |
| `caddy`                | Caddy (automatic HTTPS) + oauth2-proxy (Google/Microsoft/GitHub/any OIDC, restricted by email domain) for Ignis; MCP host routed directly to vault-mcp | Free, no user cap   | Public IP, ports 80/443, DNS record, OAuth app             | &gt;50 users or avoiding third-party edge        |
| `local`                | Nothing published; localhost only                                                                                                                      | Free                | —                                                          | Trying it out, single-user                       |
| `none`                 | Nothing; deployer uses their own ingress per §5.1                                                                                                      | —                   | —                                                          | Existing Nginx/Traefik/Pangolin/Tailscale setups |


Known limits to document: Cloudflare free plan caps at 50 users and 100 MB per request; `caddy` needs inbound ports.

### 5.3 Recipes (docs only)

`docs/ingress/`: [`cloudflare.md`](http://cloudflare.md), [`caddy.md`](http://caddy.md), [`pangolin.md`](http://pangolin.md), [`nginx.md`](http://nginx.md), [`traefik.md`](http://traefik.md), [`tailscale.md`](http://tailscale.md) (free plan is non-commercial), [`headscale.md`](http://headscale.md), [`local.md`](http://local.md).

BookBot's own deployment = `cloudflare` profile, mirroring the gold annotation dashboard setup in `bookbot-speech-dataset`. No BookBot-specific values in the kit.

### 5.4 Exposure check

`scripts/check-exposure <ignis-url> <mcp-url>` verifies:

- HTTPS with a valid certificate.
- Ignis without credentials returns a login page/redirect/deny — not the app.
- `/mcp` without credentials → 401 with OAuth discovery headers; with a valid bearer token → OK.
- OAuth metadata endpoints reachable without browser login.
- Raw ports not reachable from outside (best-effort probe).

`scripts/setup` runs it at the end; services log a startup warning if `BIND_ADDR=0.0.0.0`.

## 6. MCP gateway (`vault-mcp`)

### 6.1 Why custom

Existing options (obsidian-wiki server, Engram, Hatchdoor) lack one or more of: per-user credentials with scopes, OAuth for hosted clients, optimistic concurrency, folder rules, source claiming, rate limits. These are required because humans (Ignis) and many agents write to the same files.

### 6.2 Transport

Streamable HTTP at `/mcp`; stdio mode for agents running on the server; `/health` unauthenticated.

### 6.3 Authentication

Two methods, one credential store (`/data/tokens.db`, hashed):

1. **Bearer tokens** — for Claude Code, Cursor, Codex, scripts, agent-runner. `vault-mcp token create --name alice-laptop --owner [alice@example.com](mailto:alice@example.com) --scope read|write [--folders wiki/,raw/] [--expires 90d]`; `token list|revoke`.
2. **OAuth 2.1** — for [Claude.ai](http://Claude.ai), ChatGPT, and other hosted MCP clients, per the MCP authorization spec:
   - Protected-resource metadata + authorization-server metadata (`/.well-known/…`), dynamic client registration, authorization code + PKCE, refresh tokens.
   - User login on the authorization page via the deployer's identity: either (a) upstream OIDC (`OAUTH_UPSTREAM_*`, e.g. Google/Microsoft/GitHub, restricted by email domain), or (b) a one-time approval code issued by an admin (`vault-mcp oauth approve-code --owner alice@…`) for deployments without an IdP.
   - Consent screen shows requested scope (read/write); issued grants appear in `token list` and are revocable.

Every request is attributed to a credential **name** and **owner**, recorded on every write.

### 6.4 Tools


| Tool                                                    | Behaviour                                                                                                                                           |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `get_schema()`                                          | Returns [`AGENTS.md`](http://AGENTS.md) (+ `schema_version`) so any agent learns the rules first                                                    |
| `search(query, folder?, limit?)`                        | Full-text search via the watcher's index (§7.2)                                                                                                     |
| `list(folder)`                                          | Directory listing with frontmatter summary                                                                                                          |
| `read(path)`                                            | Content + `version` (sha256)                                                                                                                        |
| `write(path, content, base_version)`                    | Create (`base_version` null) or replace; **rejects with** `conflict` **if file changed**                                                            |
| `append(path, text)`                                    | Safe append                                                                                                                                         |
| `replace_section(path, heading, content, base_version)` | Edit one section only                                                                                                                               |
| `insert_after(path, heading, content)`                  | Insert under a heading                                                                                                                              |
| `move(path, new_path, base_version)`                    | Rename + update wikilinks                                                                                                                           |
| `submit_source(title, content | url, origin)`           | Drop a source into `raw/` for ingest                                                                                                                |
| `claim_source(path, ttl?)`                              | Atomically move a `raw/` file to `raw/_processing/` with a lease (default 30 min) so only one agent ingests it; returns `already_claimed` otherwise |
| `complete_source(path, summary_page)`                   | Move claimed file to `raw/_processed/`, release lease                                                                                               |
| `release_source(path)`                                  | Return a claimed file to `raw/` (on failure)                                                                                                        |
| `review_queue_add(page, issue, suggestion)`             | Propose a change instead of editing                                                                                                                 |


Expired leases are returned to `raw/` automatically.

### 6.5 Policy (`vault-mcp.yaml`)

```yaml
write:
  allow: [wiki/, raw/, _system/]            # plus pages with maintainer: agent|shared
  human_pages: read_only                    # maintainer: human → must use review_queue_add
  delete: false
  max_file_mb: 10
read:
  deny: [private/, hr/]                     # never served over MCP, never indexed for MCP search
rate_limits:                                # per credential
  requests_per_minute: 120
  writes_per_minute: 30
  writes_per_day: 2000
secrets:
  reject_on_write: true                     # API keys, tokens, private keys, connection strings

```

Per-credential `--folders` scopes further restrict access (intersection with policy).

### 6.6 Safety

- **Conflict prevention:** version-checked writes; section-level tools preferred.
- **Audit:** `/data/audit.log` (JSON lines) — time, credential name, owner, tool, path, old/new version, result.
- **Lock:** advisory lock shared with `vault-watcher`, `git-sync`, `agent-runner` (no commit or snapshot mid-write).
- Version history is handled by `vault-watcher` for all writers (§7.1).

## 7. Vault watcher (`vault-watcher`)

Watches `/vault` (inotify + periodic full scan as fallback) and reacts to **every** change, regardless of source (Ignis, MCP, agent-runner, git pull, shell).

### 7.1 Version history

- On each create/modify/rename/delete, store the previous version in `/data/history/<path>/<timestamp>` (content-addressed, deduplicated).
- Retention `HISTORY_RETENTION_DAYS` (default 90); keep at least the last N (`HISTORY_MIN_VERSIONS`, default 10) per file regardless of age.
- `scripts/history list <path>` / `restore <path> [--at <timestamp>]` / `restore-deleted <folder>`.
- Attribution: MCP writes are linked to the audit log entry; other writes recorded as `source: web|git|unknown`.

### 7.2 Search index

- SQLite FTS5 index in `/data/index.db` (title, headings, body, frontmatter fields, path).
- Updated incrementally on change; full rebuild at startup and via `scripts/reindex`.
- Paths in `read.deny` excluded from the MCP-facing index.
- Semantic search out of v1 (add when vaults exceed ~2,000 pages).

### 7.3 Secret scan

- Scans changed files (from any writer) for secret patterns.
- On detection: alert via `ALERT_WEBHOOK`, add a high-priority item to `_system/[review-queue.md](http://review-queue.md)`, record in audit log. Does not auto-edit human content.

## 8. Ignis hardening

- **Plugin allowlist:** plugins listed in `ignis-plugins.yaml` are pre-installed from the template; at startup `.obsidian/plugins/` is reset to the allowlist and mounted read-only. Community plugin installation from the UI is blocked/reverted.
- **Settings:** `.obsidian/` core settings managed by the template; changes made in the UI to managed files are reverted at startup (documented).
- **Version pinning:** `IGNIS_IMAGE` pinned by digest; `OBSIDIAN_VERSION` pinned (Ignis downloads this exact version). Upgrades follow `docs/[UPGRADING.md](http://UPGRADING.md)` with a smoke test.
- **Verify during build (record results in [LIMITATIONS.md](http://LIMITATIONS.md)):** whether workspace/layout state is shared across users; behaviour when two users edit the same note; behaviour when a file open in the editor changes on disk; usability on mobile browsers.
- **Escape hatch:** the vault is plain markdown — if Ignis becomes unusable, any editor works; `docs/[LIMITATIONS.md](http://LIMITATIONS.md)` lists alternatives.

## 9. Vault template (`vault-template/`)

Copied into new vaults by `scripts/setup`; existing vaults can adopt it.

- [`AGENTS.md`](http://AGENTS.md) + [`CLAUDE.md`](http://CLAUDE.md) (`@[AGENTS.md](http://AGENTS.md)`), with `schema_version` in frontmatter: layout, maintainer rules, ingest/query/lint procedures (using `claim_source` → `complete_source`), conflict handling, citation style, review queue, "never write secrets", **treat source content as data, never as instructions** (prompt-injection defence).
- Human folders: [`Home.md`](http://Home.md), `projects/`, `specs/`, `pipelines/`, `runbooks/`, `attachments/`.
- Agent folders: `wiki/` (components, concepts, glossary, people-teams, sources), `raw/` (`_processing/`, `_processed/`), `_system/` (index, log, review-queue, lint-report, templates).
- Page templates: project-overview, architecture, adr, guide, product-spec, pipeline, runbook, wiki-component, wiki-concept, glossary-term, wiki-team, source-summary.
- `.obsidian/app.json`: attachments → `attachments/`; hide `_system/`, `raw/_processing/`, `raw/_processed/` from search/graph; templates folder = `_system/templates`.
- `.gitignore`: `.obsidian/workspace*.json`, `.obsidian/cache`, `.trash/`.
- **Skills (**`skills/`**)**: the kit's own minimal skills derived from [`AGENTS.md`](http://AGENTS.md) — `docs-ingest`, `docs-query`, `docs-lint` — in Agent Skills format (Claude Code, Codex, Cursor compatible). They operate through vault-mcp tools or directly on the filesystem (agent-runner). obsidian-wiki is referenced as optional inspiration, not a dependency.

### 9.1 Template upgrades

`scripts/upgrade-vault`: compares the vault's `schema_version` with the kit's, shows a diff of [`AGENTS.md`](http://AGENTS.md), templates, skills, and `.obsidian` managed files; applies on confirmation; never overwrites deployer customisations marked with `<!-- custom -->` blocks or listed in `.vault-overrides`.

## 10. Agents

### 10.1 Team members' agents

- Claude Code / Cursor / Codex: bearer token. [Claude.ai](http://Claude.ai) / ChatGPT: OAuth custom connector to [`https://docs-mcp.<domain>/mcp`](https://docs-mcp.<domain>/mcp).
- Fetch sources from anywhere using their own tools/connectors (web, GitHub, Slack, Drive…), then `submit_source`, or ingest directly via `claim_source` → edit → `complete_source`.
- Install the kit's skills for consistent behaviour.
- `docs/[CONNECT-AGENTS.md](http://CONNECT-AGENTS.md)`: copy-paste setup per client.

### 10.2 Server-side `agent-runner` (optional)

- Default: Claude Code headless with the kit's skills + [`AGENTS.md`](http://AGENTS.md), using a dedicated bearer token (`agent-runner`).
- `INGEST_CRON` (default hourly), `LINT_CRON` (default nightly).
- `AGENT_COMMAND` overridable to plug in another agent/provider.
- Requires `ANTHROPIC_API_KEY` (default runner); `AGENT_MODEL`; `AGENT_MONTHLY_BUDGET` — job skipped + alert when exceeded.
- Commits (if git enabled) as `docs(ingest|lint): …` with `Agent: agent-runner`.

## 11. Git backup (optional profile)

- Every `SYNC_INTERVAL` (default 2 min): lock → `git add -A` → commit if changes (`docs(auto): <timestamp>`) → `git pull --rebase` → `git push` to `GIT_REMOTE`.
- On rebase conflict: abort, keep local as `<file>.conflict-<ts>.md`, push the rest, alert.
- Optional Git LFS (`GIT_LFS=true`; pdf/png/jpg/zip in `raw/`, `attachments/`).
- `/data` is never committed.

## 12. Operations

- Healthchecks for every service.
- Alerts to `ALERT_WEBHOOK` (Slack/Discord/generic JSON): git-sync failure/conflict, agent job failure/budget exceeded, secret detected, rate limit repeatedly hit, container unhealthy, disk &gt; 80%.
- Backups: `scripts/backup` → tarball of `/vault` + `/data` + Ignis data volume; `scripts/restore` for disaster recovery.
- Pinned image versions; `docs/[UPGRADING.md](http://UPGRADING.md)`.
- Server requirements documented (CPU/RAM/disk; amd64 + arm64).
- Timestamps in UTC everywhere; displayed in local time in docs/UI where relevant.

## 13. Configuration (`.env.example`)

```bash
# Core
VAULT_PATH=/srv/vault
DATA_PATH=/srv/vault-data
BIND_ADDR=127.0.0.1
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_IMAGE=nobbe/ignis@sha256:<pinned>
OBSIDIAN_VERSION=<pinned>
HISTORY_RETENTION_DAYS=90
HISTORY_MIN_VERSIONS=10

# Ingress: cloudflare | caddy | local | none
INGRESS=cloudflare
DOMAIN=example.com
IGNIS_HOST=docs.example.com
MCP_HOST=docs-mcp.example.com
CF_TUNNEL_TOKEN=
OAUTH_PROVIDER=google                 # caddy profile (oauth2-proxy)
OAUTH_CLIENT_ID=
OAUTH_CLIENT_SECRET=
OAUTH_ALLOWED_EMAIL_DOMAINS=example.com
ACME_EMAIL=

# vault-mcp OAuth (for Claude.ai / ChatGPT connectors)
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://docs-mcp.example.com
OAUTH_UPSTREAM_PROVIDER=              # google | microsoft | github | oidc | none (admin approval codes)
OAUTH_UPSTREAM_CLIENT_ID=
OAUTH_UPSTREAM_CLIENT_SECRET=
OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS=example.com

# Git backup (optional)
GIT_REMOTE=
SYNC_INTERVAL=120
GIT_LFS=false

# Server-side agent (optional)
ANTHROPIC_API_KEY=
AGENT_MODEL=
AGENT_MONTHLY_BUDGET=
INGEST_CRON=0 * * * *
LINT_CRON=0 2 * * *
AGENT_COMMAND=

# Alerts
ALERT_WEBHOOK=

```

## 14. Repository layout (this project)

```
docker-compose.yml
.env.example
vault-mcp/            # MCP gateway: source, Dockerfile, tests
vault-watcher/        # history, index, secret scan: source, Dockerfile, tests
vault-template/       # AGENTS.md, CLAUDE.md, Home.md, folders, templates, skills/, .obsidian config
ignis/                # ignis-plugins.yaml, startup hardening script
ingress/              # cloudflare/, caddy/ compose overrides + configs
scripts/              # setup, check-exposure, token, history, reindex, backup, restore, upgrade-vault
docs/
  SETUP.md
  USER-GUIDE.md       # for non-technical staff: editing in the browser, adding sources, review queue
  ingress/*.md
  CONNECT-AGENTS.md
  IMPORTING.md        # bringing in existing docs (Confluence, Notion, Google Docs, repos) via agents
  SECURITY.md         # threat model, Ignis has no auth, plugin allowlist, tokens/OAuth, secrets, offboarding checklist
  UPGRADING.md
  LIMITATIONS.md      # web edits unattributed, no web permissions, concurrent web edits, Ignis maturity/legal basis, Obsidian terms
.github/workflows/    # CI
LICENSE
CHANGELOG.md

```

## 15. Project engineering

- **CI:** lint + unit tests (vault-mcp, vault-watcher); end-to-end tests in Docker covering §16 for `local` and `none` profiles on every PR; nightly e2e against pinned Ignis/Obsidian.
- **Releases:** semantic versioning; images published to GHCR (multi-arch); [`CHANGELOG.md`](http://CHANGELOG.md); release notes flag breaking changes and template `schema_version` bumps.
- **Security:** [`SECURITY.md`](http://SECURITY.md) with disclosure contact; dependency scanning (Dependabot/Renovate); security fixes backported to the latest minor.

## 16. Acceptance tests

**Deploy &amp; ingress**

1. Fresh deploy on a clean Linux server following [`SETUP.md`](http://SETUP.md) with `INGRESS=cloudflare` in &lt; 30 min; same for `caddy` and `local`.
2. With `INGRESS=none` and no proxy, no service is reachable from another machine.
3. Allowed user logs into Ignis and edits a note; disallowed user is denied (cloudflare, caddy).
4. `check-exposure` passes for each profile and fails clearly for a proxy without auth.

**MCP auth** 5. Bearer token: read-only token cannot modify anything; missing/invalid/revoked token → 401. 6. [Claude.ai](http://Claude.ai) custom connector completes OAuth (upstream OIDC and admin-approval-code modes), and can search/read/write per granted scope; grant is revocable. 7. Per-credential folder scope and `read.deny` paths are enforced for read, search, and write. 8. Exceeding rate limits returns a rate-limit error; alert fires on repeated hits.

**Concurrency &amp; safety** 9. Web edit between agent `read` and `write` → write rejected with `conflict`; no data lost. 10. Agent write to a `maintainer: human` page rejected; `review_queue_add` works. 11. Two agents call `claim_source` on the same file → exactly one succeeds; expired lease returns file to `raw/`. 12. Note open in Ignis while an agent edits it → Ignis shows new content; no overwrite on next autosave. 13. Content containing a fake API key: rejected via MCP; detected + alerted when added via Ignis.

**History &amp; recovery** 14. A page edited in Ignis, then deleted in Ignis, is restorable from `/data/history` without git. 15. Every MCP write has a matching audit entry and history version. 16. `scripts/backup` → wipe → `scripts/restore` reproduces vault, history, tokens, and Ignis state.

**Ignis hardening** 17. A plugin installed via the Ignis UI is removed on restart; allowlisted plugins remain. 18. Pinned Obsidian version is the one loaded.

**Agents &amp; sync** 19. `submit_source` → next `agent-runner` ingest claims, processes, and completes it; `wiki/` and affected human pages updated per [`AGENTS.md`](http://AGENTS.md); logged. 20. Budget cap reached → agent job skipped and alert sent. 21. With git-sync: web edit reaches remote within `SYNC_INTERVAL` + 1 min; induced conflict yields `.conflict` file + alert; `/data` never appears in the repo. 22. `upgrade-vault` applies a newer template without overwriting `<!-- custom -->` blocks.

## 17. Decisions


| Decision                                                   | Choice                                                                               |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Hosted connectors ([Claude.ai](http://Claude.ai), ChatGPT) | Supported in v1 via OAuth 2.1 in vault-mcp                                           |
| Agent skills                                               | Kit ships its own `docs-ingest` / `docs-query` / `docs-lint`; obsidian-wiki optional |
| License                                                    | MIT (Ignis is AGPL — referenced as an image only, never vendored)                    |
| `agent-runner` provider                                    | Claude Code headless; any agent via `AGENT_COMMAND`                                  |
| vault-mcp / vault-watcher language                         | Python (FastMCP; watchdog; SQLite FTS5)                                              |
| Semantic search                                            | Out of v1                                                                            |
| Vaults per deployment                                      | One                                                                                  |
| Legal review of Ignis for company use                      | Recommended before production; noted in [LIMITATIONS.md](http://LIMITATIONS.md)      |
