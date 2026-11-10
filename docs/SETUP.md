# Setting up the Docs Vault Kit

This guide takes you from an empty Linux server to a running vault: people edit in the browser (Obsidian through Ignis), agents read and write over MCP, and every change is versioned. It assumes you are comfortable with a Linux shell and Docker. Plan on about 30 minutes, most of it spent on DNS, the tunnel or the login app.

Contents:

1. [Server requirements](#1-server-requirements)
2. [Install Docker](#2-install-docker)
3. [Get the kit](#3-get-the-kit)
4. [Choose how people reach it (INGRESS)](#4-choose-how-people-reach-it-ingress)
5. [Run scripts/setup](#5-run-scriptssetup)
6. [Check the deployment](#6-check-the-deployment)
7. [Agent access: bearer tokens](#7-agent-access-bearer-tokens)
8. [Agent access: OAuth for Claude.ai and ChatGPT](#8-agent-access-oauth-for-claudeai-and-chatgpt)
9. [Optional: git backup (git-sync)](#9-optional-git-backup-git-sync)
10. [Optional: server-side agent (agent-runner)](#10-optional-server-side-agent-agent-runner)
11. [Alerts](#11-alerts)
12. [Backups](#12-backups)
13. [Everyday commands](#13-everyday-commands)
14. [Troubleshooting](#14-troubleshooting)

What runs (all in Docker Compose, project name `docs-vault`):

| Service | What it does | When |
|---|---|---|
| `ignis-init` | One-shot: enforces the plugin allowlist and the managed `.obsidian` settings, then exits | every start |
| `ignis` | The Obsidian web editor, on `127.0.0.1:${IGNIS_PORT}` (8081) | always |
| `vault-mcp` | The MCP gateway for agents, on `127.0.0.1:${MCP_PORT}` (8082) | always |
| `vault-watcher` | Version history, search index, secret scan, health monitor | always |
| `cloudflared` | Cloudflare Tunnel | `INGRESS=cloudflare` |
| `caddy`, `oauth2-proxy` | HTTPS and editor login | `INGRESS=caddy` |
| `git-sync` | Pushes the vault to a git remote | `GIT_REMOTE` set |
| `agent-runner` | Scheduled ingest and lint jobs | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or `AGENT_COMMAND` set |

Two host folders hold all state:

- `VAULT_PATH` (default `/srv/vault`): the vault itself, plain Markdown.
- `DATA_PATH` (default `/srv/vault-data`): history, audit log, search index, tokens and OAuth grants, Ignis state. Keep it **outside** the vault. It is never served by Ignis and never committed by git-sync.

## 1. Server requirements

**Operating system:** a Linux server (any distribution that runs current Docker Engine). `VAULT_PATH` and `DATA_PATH` must be on a local filesystem of that server: the services share a file lock (`flock`) on `DATA_PATH` and watch `VAULT_PATH` with inotify, so don't put them on NFS or another network share.

**CPU architecture:** amd64 (x86_64) or arm64 (aarch64). The third-party images the kit uses are published for both, pinned by multi-arch digest:

| Image | Used by | amd64 | arm64 |
|---|---|---|---|
| `nobbe/ignis` (pinned digest) | `ignis` | yes | yes |
| `python:3.13-slim` (pinned digest) | base of `vault-mcp`, `vault-watcher`, `ignis-init` | yes | yes |
| `node:22-slim` (pinned digest) | base of `agent-runner` | yes | yes |
| `alpine:3.22` (pinned digest) | base of `git-sync` | yes | yes |
| `caddy` 2.10.2-alpine (pinned digest) | `caddy` | yes | yes |
| `cloudflare/cloudflared` 2026.10.5 (pinned digest) | `cloudflared` | yes | yes |
| `quay.io/oauth2-proxy/oauth2-proxy` v7.12.0-alpine (pinned digest) | `oauth2-proxy` | yes | yes |

The kit's own images (`vault-mcp`, `vault-watcher`, `git-sync`, `agent-runner`) are built on your server from this repository the first time you start it, so they match your architecture. The kit's builds and end-to-end tests have run on amd64; arm64 builds have not been run yet.

**Sizing.** These are starting points, not measured limits:

- **CPU:** 2 vCPUs. Idle load is near zero; CPU is used mostly by image builds, the search index rebuild at startup, and the server-side agent if you turn it on.
- **Memory:** 2 GB is enough for the core services. Measured at idle on a test stack: Ignis about 225 MiB, vault-mcp about 70 MiB, vault-watcher about 30 MiB. Obsidian itself runs in each user's browser, not on the server. Add about 1 GB if you enable `agent-runner` (it runs Claude Code, a Node.js program, during jobs).
- **Disk:** about 1 GB for images (vault-mcp ~210 MB, vault-watcher ~135 MB, Ignis ~305 MB, agent-runner ~600 MB if enabled, plus the ingress images), plus the vault, plus history. History stores each version of each file compressed and de-duplicated, kept for `HISTORY_RETENTION_DAYS` (90) and at least `HISTORY_MIN_VERSIONS` (10) per file, so budget a few times the vault's size for `DATA_PATH`. The audit log is not rotated (about 0.3 KB per MCP call). vault-watcher alerts when `VAULT_PATH` or `DATA_PATH` is more than 80 % full.

**Network:** outbound internet access, at least on first start: Docker pulls images and builds the kit's images (Python, npm and apt packages), Ignis downloads Obsidian on its first start, and `ignis-init` downloads any allowlisted plugins from GitHub. Inbound ports depend on the ingress (section 4); the kit itself only listens on `127.0.0.1`.

## 2. Install Docker

You need Docker Engine with the **Compose v2 plugin** (`docker compose`, not the old `docker-compose`). Follow Docker's instructions for your distribution: https://docs.docker.com/engine/install/

Check:

```bash
docker version
docker compose version      # must print "Docker Compose version v2..."
```

The host also needs `bash`, `git`, `curl` and `openssl`. Everything else runs inside containers.

Run the kit as an ordinary user who can use Docker (for example, a member of the `docker` group). `scripts/setup` records that user's uid and gid as `PUID`/`PGID`, and every container writes vault files as that user, so the vault and data folders must belong to it. If the folders live somewhere your user can't create directories, create them first:

```bash
sudo mkdir -p /srv/vault /srv/vault-data
sudo chown "$(id -u):$(id -g)" /srv/vault /srv/vault-data
```

Membership of the `docker` group is equivalent to root on the host. Give it only to administrators.

## 3. Get the kit

```bash
git clone https://github.com/DavidSamuell/llm-second-brain.git docs-vault-kit
cd docs-vault-kit
```

Run every command in this guide from this folder. `docker compose` reads `.env` from the current directory, and the scripts change into the repository root themselves.

## 4. Choose how people reach it (INGRESS)

The kit never publishes anything to the internet by itself. You choose one `INGRESS` value:

| `INGRESS` | What the kit runs | What you need first | Guide |
|---|---|---|---|
| `cloudflare` | `cloudflared` (Cloudflare Tunnel). Editor behind Cloudflare Access; MCP host bypasses Access. | A domain on Cloudflare; Zero Trust enabled (free up to 50 users); a tunnel token; two public hostnames in the tunnel; an Access app (allow) for the editor host and a Bypass app for the MCP host. No inbound ports. Upload limit 100 MB per request. | [ingress/cloudflare.md](ingress/cloudflare.md) |
| `caddy` | Caddy (Let's Encrypt HTTPS) and oauth2-proxy (editor login). | A public IP with ports 80 and 443 open; DNS `A`/`AAAA` records for both hostnames; an OAuth app at Google, Microsoft, GitHub or any OIDC provider with redirect URL `https://<IGNIS_HOST>/oauth2/callback`; an email address for Let's Encrypt. | [ingress/caddy.md](ingress/caddy.md) |
| `local` | Nothing. Editor and MCP listen on `127.0.0.1` only; you connect over an SSH tunnel. | SSH access to the server. No domain. Claude.ai and ChatGPT connectors can't reach it. | [ingress/local.md](ingress/local.md) |
| `none` | Nothing. You put your own proxy, tunnel or VPN in front. | A proxy that meets the contract below. | Recipes: [nginx.md](ingress/nginx.md), [traefik.md](ingress/traefik.md), [pangolin.md](ingress/pangolin.md), [tailscale.md](ingress/tailscale.md) (free plan is non-commercial), [headscale.md](ingress/headscale.md) |

All profiles except `local` use two hostnames: `IGNIS_HOST` for people (for example `docs.example.com`) and `MCP_HOST` for agents (for example `docs-mcp.example.com`).

Whatever you use in front of the kit must:

1. terminate HTTPS (Ignis needs a secure context);
2. pass WebSockets on the editor host;
3. require a login before anyone reaches the editor (Ignis has no login of its own, and anyone who reaches it can edit everything);
4. route the whole MCP host **without** a browser login, including `/mcp`, `/.well-known/oauth-*`, `/authorize`, `/token`, `/register`, `/revoke` and `/oauth/*` (vault-mcp authenticates every request itself);
5. never expose `IGNIS_PORT` or `MCP_PORT` directly. Keep `BIND_ADDR=127.0.0.1`. Ports published by Docker on `0.0.0.0` bypass host firewalls such as ufw.

Do the preparation in your guide (tunnel, DNS, OAuth app, Access apps) up to the "Configure the kit" step, then come back here.

If you just want to try the kit, use `local`: it needs nothing but this server.

## 5. Run scripts/setup

### Interactive

```bash
scripts/setup
```

Setup asks for each setting and shows the current value in brackets; Enter keeps it. The questions depend on the profile:

| Question | Setting | Asked when |
|---|---|---|
| Vault folder on this server | `VAULT_PATH` (absolute path) | always |
| Data folder (history, tokens, search index) | `DATA_PATH` (absolute path, outside the vault) | always |
| How people reach it | `INGRESS` | always |
| Your domain, editor hostname, MCP hostname | `DOMAIN`, `IGNIS_HOST`, `MCP_HOST` | `cloudflare`, `caddy`, `none` |
| Cloudflare tunnel token | `CF_TUNNEL_TOKEN` (hidden input) | `cloudflare` |
| Email for Let's Encrypt; editor login provider, client id, secret, allowed email domains | `ACME_EMAIL`, `OAUTH_PROVIDER`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_ALLOWED_EMAIL_DOMAINS` | `caddy` |
| Agent connector sign-in provider (and its client id, secret, allowed domains) | `OAUTH_UPSTREAM_*` (section 8) | not `local` |
| Git remote for backups | `GIT_REMOTE` (empty = off; section 9) | always |
| Anthropic API key for the server-side agent | `ANTHROPIC_API_KEY` (empty = off; section 10) | always |
| Alert webhook URL | `ALERT_WEBHOOK` (empty = off; section 11) | always |

Settings that setup doesn't ask for (for example `OAUTH_UPSTREAM_TENANT`, `OAUTH_UPSTREAM_ISSUER`, `AGENT_MONTHLY_BUDGET`, `AGENT_COMMAND`, `SYNC_INTERVAL`) are edited in `.env` directly; then rerun setup. Every setting is listed with a comment in [`.env.example`](../.env.example).

### Non-interactive

Write `.env` yourself and let setup use it as it is, without prompts:

```bash
cp .env.example .env
chmod 600 .env
"${EDITOR:-nano}" .env        # set at least VAULT_PATH, DATA_PATH, INGRESS and the profile's values
scripts/setup --non-interactive
```

Minimal `.env` changes for a `local` trial:

```bash
VAULT_PATH=/srv/vault
DATA_PATH=/srv/vault-data
INGRESS=local
MCP_OAUTH_ENABLED=false       # no public MCP host, so no OAuth connectors
```

The ingress guides show the values for `cloudflare`, `caddy` and each `none` recipe.

### Options

| Option | Effect |
|---|---|
| `--non-interactive` | Use `.env` as it is; never prompt. Fails with a message if a required value is missing. |
| `--reconfigure` | Interactive (the default); use it after changing `INGRESS`. |
| `--env-file PATH` | Read and write another file instead of `.env`. Plain `docker compose` and the other scripts read `.env` only, so keep the default unless you have a reason. |
| `--no-start` | Write the configuration, folders and vault, but don't start the services. |
| `--adopt` | The vault folder already has content but no `AGENTS.md`: copy the kit's template files that don't exist yet, without asking. Existing files are never overwritten. |

### What setup does

1. Checks that `docker` and the `docker compose` plugin are installed.
2. Validates the settings: `VAULT_PATH` and `DATA_PATH` must be absolute; for every profile except `local`, `IGNIS_HOST` and `MCP_HOST` must be real hostnames (setup refuses anything containing `example.com`); `cloudflare` needs `CF_TUNNEL_TOKEN`; `caddy` needs `ACME_EMAIL`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` and `OAUTH_ALLOWED_EMAIL_DOMAINS`.
3. Derives values: `PUID`/`PGID` from your user; `OAUTH_COOKIE_SECRET` for `caddy` (generated once); `MCP_OAUTH_ISSUER=https://<MCP_HOST>` if it is empty and `MCP_OAUTH_ENABLED=true` (not for `local`); `COMPOSE_FILE` (the base file plus `ingress/<INGRESS>/compose.yml` for `cloudflare` and `caddy`, plus `compose.plugins.yml` if it exists); `COMPOSE_PROFILES` (`git` when `GIT_REMOTE` is set, `agent` when `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or `AGENT_COMMAND` is set).
4. Writes `.env` (mode 600) in the layout of `.env.example`. Settings that aren't in `.env.example` are kept at the end.
5. Copies `vault-mcp.example.yaml` to `vault-mcp.yaml` if that file doesn't exist. This is the agent policy: which folders agents may write, which they may never read, rate limits. Edit it to taste; restart `vault-mcp` and `vault-watcher` after changes.
6. Creates `VAULT_PATH` and the folders under `DATA_PATH`, and checks your user can write them.
7. Prepares the vault:
   - empty folder: copies `vault-template/` into it (AGENTS.md, CLAUDE.md, Home.md, folders, page templates, skills, `.obsidian` settings);
   - folder with `AGENTS.md`: leaves it alone (template updates go through `scripts/upgrade-vault`, see [UPGRADING.md](UPGRADING.md));
   - folder with other content: asks whether to add the missing template files (or does so with `--adopt`). If you decline, the vault runs without `AGENTS.md` and agents won't know its rules.
8. Creates the `agent-runner` bearer token if that profile is on (section 10), and an SSH deploy key if `GIT_REMOTE` is an SSH remote (section 9).
9. Starts the stack with `docker compose up -d --build --wait` and waits up to 15 minutes for every service to be healthy. The first start builds the kit's images and downloads Obsidian, which takes a few minutes.
10. Except for `local`: creates a temporary read token, runs `scripts/check-exposure https://<IGNIS_HOST> https://<MCP_HOST>`, revokes the token, and stops with an error if any check fails.

At the end setup prints the editor's address. Setup is safe to rerun: existing settings, vault content and tokens are kept.

With `INGRESS=none`, the exposure check at the end tests your own proxy. If the proxy isn't configured yet, setup reports failures after the stack has started; finish the proxy and run the check again (section 6).

## 6. Check the deployment

**Services:**

```bash
docker compose ps
```

Every service should show `healthy`. `ignis-init` runs once before Ignis and exits; `docker compose ps -a` lists it as exited with code 0. On the server:

```bash
curl -s http://127.0.0.1:8082/health          # vault-mcp: {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/   # Ignis: 200
```

**Exposure check** (every profile except `local`):

```bash
token=$(scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1h)
CHECK_TOKEN=$token scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
scripts/token revoke exposure-check
```

It checks: valid HTTPS on both hosts; the editor is not served to anonymous visitors; `/mcp` answers 401 with OAuth discovery without a token and accepts the token; the OAuth metadata is public; `IGNIS_PORT`/`MCP_PORT` are closed on the server's public addresses. Each line prints PASS, FAIL or SKIP, and the exit code is the number of failures. Pass the token through `CHECK_TOKEN`, never as an argument.

Useful environment variables for the check:

| Variable | Use |
|---|---|
| `CHECK_SKIP=ignis-login` | The editor is only reachable inside a tailnet this server belongs to (Tailscale, Headscale). Run the login check from a machine outside it. |
| `CHECK_PUBLIC_IPS="203.0.113.10"` | Addresses to probe for the raw ports instead of the server's own public addresses. |
| `CHECK_CA_FILE=/path/ca.pem` | A private CA for your certificates. |
| `ENV_FILE=/path/.env` | Read `IGNIS_PORT`, `MCP_PORT` and `MCP_OAUTH_ENABLED` from another file. |

The raw-port probe only sees this server's own addresses. Also probe from another machine:

```bash
nc -zv <server-address> 8081 8082      # both must fail
```

**For `local`:** setup doesn't run the exposure check (there are no public URLs). From another machine, `curl -m 5 http://<server-ip>:8081/` and `curl -m 5 http://<server-ip>:8082/health` must both fail. Then open an SSH tunnel and the editor as described in [ingress/local.md](ingress/local.md).

**Login:** open `https://<IGNIS_HOST>` in a private browser window. You should get your login page (Cloudflare Access, oauth2-proxy, or your proxy's), and after signing in the editor. Edit a note and check it saves. Then try an account that isn't allowed: it must be refused.

**Obsidian version:** the Ignis log names the Obsidian version it loaded. It must equal `OBSIDIAN_VERSION` in `.env`:

```bash
docker compose logs ignis | grep -E 'Obsidian (ready|already set up)'
```

**Community plugins:** the kit ships with an empty plugin allowlist ([`ignis/ignis-plugins.yaml`](../ignis/ignis-plugins.yaml)), and plugins can't be installed from the editor. To allow a plugin, pin its release and file hashes in that file as its comments describe, then restart (`docker compose up -d`). Each person must still switch community plugins on once in their own browser, because Obsidian stores that per browser.

## 7. Agent access: bearer tokens

Claude Code, Cursor, Codex, scripts and agents on the server use bearer tokens. Create one per person and device:

```bash
scripts/token create --name alice-laptop --owner alice@example.com --scope write
scripts/token create --name ci-reader --owner ops@example.com --scope read --folders wiki/ --expires 30d
scripts/token list
scripts/token revoke alice-laptop
```

- The token (`vmcp_…`) is printed once, on standard output. Only its hash is stored, so copy it now.
- `--scope read|write` is required; `write` includes read.
- `--folders wiki/,raw/` limits the token to those folders for read, list, search and write.
- `--expires` defaults to `90d`; accepted forms are `<n>d`, `<n>h`, `<n>m` or `never`.
- `scripts/token list` shows bearer tokens and OAuth grants; `revoke` takes a name or id and works on both.

The MCP endpoint is `https://<MCP_HOST>/mcp` (or `http://127.0.0.1:8082/mcp` through the SSH tunnel for `local`). Client-by-client setup is in [CONNECT-AGENTS.md](CONNECT-AGENTS.md).

## 8. Agent access: OAuth for Claude.ai and ChatGPT

Hosted clients (Claude.ai, ChatGPT custom connectors) can't use a pasted bearer token; they sign in with OAuth. vault-mcp runs its own OAuth server. It is on only when both are true:

- `MCP_OAUTH_ENABLED=true` (the default), and
- `MCP_OAUTH_ISSUER` is set to the public origin of the MCP host: `https://` plus `MCP_HOST`, no path, no trailing slash. Setup fills it in for every profile except `local`.

With `MCP_OAUTH_ENABLED=true` and an empty issuer, vault-mcp logs a warning and accepts bearer tokens only. That is the normal state for `local`. Choose the issuer once: connectors added under an old issuer have to be added again.

People sign in to vault-mcp in one of two ways, chosen with `OAUTH_UPSTREAM_PROVIDER`.

### Option A: approval codes (`OAUTH_UPSTREAM_PROVIDER=none`)

No identity provider needed. An admin creates a one-time code for a person, and the person enters it on vault-mcp's sign-in page when connecting the app:

```bash
scripts/oauth approve-code --owner alice@example.com --scope write --ttl 1h
```

- The code (format `XXXXX-XXXXX`) is printed on standard output; send it to the person over a trusted channel.
- `--scope` defaults to `read`; give `--scope write` explicitly. On the consent page the person can grant less than the code allows, never more.
- `--ttl` is between 1 minute and 7 days (default `1h`). Each code works once.
- `scripts/oauth` runs the command inside the vault-mcp container (`docker compose exec` when the stack is running, a temporary container otherwise).

### Option B: sign in with your identity provider

Set `OAUTH_UPSTREAM_PROVIDER` to `google`, `microsoft`, `github` or `oidc`, and create an OAuth app at that provider with this redirect URI:

```
https://<MCP_HOST>/oauth/callback
```

This is a second app, separate from the editor login app used by `caddy` (whose redirect is `https://<IGNIS_HOST>/oauth2/callback`).

```bash
OAUTH_UPSTREAM_PROVIDER=google
OAUTH_UPSTREAM_CLIENT_ID=...
OAUTH_UPSTREAM_CLIENT_SECRET=...
OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS=example.com
```

Rules vault-mcp enforces (it refuses to start if the settings break them, and logs `error: ...`):

- `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS` must list at least one domain. Only verified emails in those domains can sign in.
- `OAUTH_UPSTREAM_CLIENT_ID` and `OAUTH_UPSTREAM_CLIENT_SECRET` are required.
- **google:** only Google Workspace accounts get in. The ID token must carry the Workspace `hd` claim, and it must equal the email's domain. Personal Google accounts are refused, including personal accounts registered with a work address. The email must be verified.
- **microsoft:** set `OAUTH_UPSTREAM_TENANT` to your Entra tenant id or tenant domain (for example `example.com`). `common`, `organizations` and `consumers` are refused, because a multi-tenant login would accept any tenant's self-asserted email. Within the tenant, the `email` claim (or `preferred_username`) is trusted. Guest accounts in your tenant can sign in too; turn on "Assignment required" for the app in Entra to limit who can.
- **github:** an OAuth app; only the account's primary, verified email counts.
- **oidc:** set `OAUTH_UPSTREAM_ISSUER` to the provider's `https://` issuer URL. It must match the `issuer` value in the provider's `/.well-known/openid-configuration` exactly (including any trailing slash).

When a provider is set, the approval-code form is turned off. Anyone who signs in with an allowed email may grant write access (they can narrow it to read on the consent page).

After changing these settings, run `docker compose up -d` (or rerun setup) and check `docker compose logs vault-mcp`.

OAuth grants show up in `scripts/token list` with names like `oauth:<client name>-<id>`, owned by the person's email, and are revoked with `scripts/token revoke <name-or-id>`.

## 9. Optional: git backup (git-sync)

Set `GIT_REMOTE` and rerun setup. That adds the `git` profile, and the `git-sync` service then commits and pushes the vault every `SYNC_INTERVAL` seconds (default 120). `DATA_PATH` is never part of the repository.

```bash
GIT_REMOTE=git@github.com:example/docs-vault.git
SYNC_INTERVAL=120
GIT_LFS=false        # true: PDFs, PNGs, JPGs and ZIPs under raw/ and attachments/ go to Git LFS
```

**SSH remotes** (`git@…` or `ssh://…`): setup creates a deploy key at `DATA_PATH/git-sync/ssh/id_ed25519`, records the git host's key in `DATA_PATH/git-sync/ssh/known_hosts`, and prints the public key. Add it to the repository as a deploy key **with write access** (GitHub: repository → Settings → Deploy keys). Interactive setup waits for you to press Enter; with `--non-interactive`, add the key before git-sync's first push. If setup couldn't fetch the host key, add it to `known_hosts` yourself.

**HTTPS remotes:** put the credentials in the URL, for example `https://<user>:<token>@git.example.com/example/docs-vault.git`. They are stored in `.env` (mode 600).

**Existing remote content:** use an empty repository. If the remote already has the branch (default `main`), git-sync refuses to mix histories: it sends an alert and stops. To rebase the vault onto an existing branch on purpose, add `GIT_ADOPT_REMOTE=true` to `.env` and run `docker compose up -d`. `GIT_BRANCH` (default `main`) can be added the same way.

On a conflict, git-sync keeps your local copy as `<file>.conflict-<timestamp>.md`, takes the remote side of the conflicting lines, pushes, and sends an alert. Conflicts it can't resolve pause pushing until you fix the repository.

## 10. Optional: server-side agent (agent-runner)

`agent-runner` runs the kit's ingest and lint jobs on a schedule, through vault-mcp with its own token. It has no direct write access to the vault. The default Claude Code runner may use only the vault-mcp tools and Claude Code's `WebFetch` tool, which it uses to fetch sources that were submitted as a URL only. Fetched pages are treated as data, never as instructions, but they are still untrusted input reaching a model with write access to the vault; see [SECURITY.md](SECURITY.md). Turn it on with either:

- `ANTHROPIC_API_KEY=...`: runs Claude Code headless (the default), billed per use, or
- `CLAUDE_CODE_OAUTH_TOKEN=...`: runs Claude Code headless on a Claude subscription. Create the token on any machine where `claude` is logged in to that account with `claude setup-token`. Jobs then count against that account's usage limits (shared with its interactive use), and when the limit is reached jobs fail with an alert until it resets, or
- `AGENT_COMMAND=...`: runs your own command instead.

Then rerun setup. Setup adds the `agent` profile and creates the `agent-runner` token (scope `write`, owner `agent-runner@<DOMAIN>`, never expires), saved as `AGENT_RUNNER_TOKEN` in `.env`. Don't set that token by hand.

| Setting | Meaning |
|---|---|
| `AGENT_MODEL` | Model passed to Claude Code (`--model`); empty = Claude Code's default |
| `AGENT_MONTHLY_BUDGET` | USD per UTC month. When this month's spend reaches it, jobs are skipped and an alert is sent. **Empty = no cap** (the service logs a warning). |
| `INGEST_CRON` | Ingest schedule, cron syntax, UTC (default `0 * * * *`, hourly) |
| `LINT_CRON` | Lint schedule (default `0 2 * * *`, nightly) |
| `INGEST_MAX_SOURCES` | Most sources one ingest run processes (default `10`) |
| `AGENT_MAX_TURNS` | Claude Code's `--max-turns` per job (default `200`) |

A custom `AGENT_COMMAND` receives `VAULT_MCP_URL`, `VAULT_MCP_TOKEN` and `JOB=ingest|lint` in its environment. The budget is enforced only if the command prints a JSON line with `total_cost_usd`; otherwise the service logs that the budget isn't enforced.

Jobs never overlap. To run one now:

```bash
docker compose exec agent-runner run-job ingest
docker compose logs agent-runner
```

The runner mounts the vault's `skills/` folder read-only; the template creates it. With git-sync on, agent commits are named `docs(ingest): …` or `docs(lint): …` with the trailer `Agent: agent-runner`.

## 11. Alerts

Set `ALERT_WEBHOOK` to a Slack incoming webhook, a Discord webhook, or any URL that accepts a JSON POST. The payload format is picked from the URL (`hooks.slack.com` → Slack, `discord.com/api/webhooks` → Discord, anything else → generic JSON with `event`, `severity`, `service`, `message`, `details`, `ts`).

Alerts include: a service unhealthy for 3 checks in a row (and its recovery), disk over 80 % on the vault or data folder, a secret detected in the vault, a credential repeatedly hitting rate limits, git-sync conflicts and failures, agent-runner failures and budget stops, approval-code guessing, and reuse of an OAuth refresh token. Repeats of the same alert are suppressed for an hour. `MONITOR_INTERVAL_S` (default 60) sets how often vault-watcher checks the services.

After setting it, run `docker compose up -d` (or rerun setup).

## 12. Backups

`scripts/backup` takes a backup while the stack is running. Writes pause briefly while it holds the vault lock.

```bash
scripts/backup                          # ./backups/vault-backup-<UTC time>.tar.gz
scripts/backup --out /mnt/backups       # another folder
scripts/backup --include-config         # also .env and vault-mcp.yaml (they contain secrets)
```

The archive (mode 600) holds the vault and `DATA_PATH`: history, tokens and OAuth grants, leases, audit log, search index and Ignis state, with a manifest of checksums. Store it off the server. A daily host cron job is enough for most teams, for example:

```bash
# crontab -e
0 3 * * * cd /path/to/docs-vault-kit && scripts/backup --out /mnt/backups >> /var/log/docs-vault-backup.log 2>&1
```

Old archives are not deleted for you.

To restore on the same server:

```bash
docker compose down
scripts/restore /mnt/backups/vault-backup-20261110T030000Z.tar.gz          # VAULT_PATH and DATA_PATH must be empty
scripts/restore /mnt/backups/vault-backup-20261110T030000Z.tar.gz --force  # or: replace their contents
docker compose up -d
```

Restore checks every file against the manifest before writing anything and rebuilds the search index. If the archive contains `.env` or `vault-mcp.yaml`, they are put in `./restored-config/` for you to compare; your current files are never overwritten.

To restore onto a new server, use the `.env` and `vault-mcp.yaml` from the old one (back up with `--include-config`, or keep copies elsewhere). The `AGENT_RUNNER_TOKEN` in that `.env` matches the restored token store; a fresh `scripts/setup` would create a new token and copy the template into the vault first.

```bash
# after sections 2 and 3 (Docker installed, kit cloned, empty folders owned by your user)
tar -xzf /mnt/backups/vault-backup-20261110T030000Z.tar.gz config/.env config/vault-mcp.yaml
mv config/.env .env && mv config/vault-mcp.yaml vault-mcp.yaml && rmdir config
chmod 600 .env
scripts/restore /mnt/backups/vault-backup-20261110T030000Z.tar.gz
scripts/setup --non-interactive      # updates PUID/PGID for this server, starts, runs the exposure check
```

Change `VAULT_PATH` and `DATA_PATH` in `.env` first if the new server uses other folders.

## 13. Everyday commands

```bash
docker compose ps                                   # status
docker compose logs -f vault-mcp                    # logs of one service
docker compose up -d                                # apply .env changes
docker compose restart vault-mcp vault-watcher      # apply vault-mcp.yaml changes
scripts/history list wiki/index.md                  # versions of a file
scripts/history restore wiki/index.md --at <timestamp>
scripts/history restore-deleted wiki/               # bring back deleted files in a folder
scripts/reindex                                     # rebuild the search index
```

Upgrades: see [UPGRADING.md](UPGRADING.md).

## 14. Troubleshooting

**`docker is not installed` / `the docker compose plugin is not installed`**
Install Docker Engine and the Compose v2 plugin (section 2). `docker-compose` (v1) is not supported.

**`set IGNIS_HOST to your real hostname`**
For `cloudflare`, `caddy` and `none`, the hostnames can't be left at `*.example.com`. For a trial without a domain, use `INGRESS=local`.

**`<folder> is not writable by <user>; fix its owner`**
The vault and data folders must belong to the user who runs setup (section 2): `sudo chown -R "$(id -u):$(id -g)" /srv/vault /srv/vault-data`.

**`some services did not become healthy`**
Setup prints `docker compose ps`. Look at the unhealthy service's log, `docker compose logs <service>`. Common causes:
- `ignis-init` exited non-zero: a plugin in `ignis/ignis-plugins.yaml` failed to download or its sha256 doesn't match.
- `ignis` still starting: the first start downloads Obsidian. Wait and run `docker compose up -d --wait` again.
- `vault-mcp` exits with `error: OAUTH_UPSTREAM_...`: the OAuth settings break a rule in section 8.
- `vault-mcp` or `vault-watcher` exits on the policy file: `vault-mcp.yaml` has a syntax or validation error; the log names it.
- `cloudflared` unhealthy: wrong or revoked `CF_TUNNEL_TOKEN` (`Unauthorized` in its log).
- `caddy` can't get certificates: DNS must point at the server and port 80 must be open.

**Exposure check failures**
- `https: … not HTTPS` or a certificate error: the proxy doesn't terminate TLS for that host, or DNS points elsewhere.
- `ignis-login: … serves the editor without login`: the editor host isn't behind your login. Fix this before sharing the URL.
- `mcp-auth: … answered 302/403`: the MCP host is behind a browser login or a WAF challenge. Route it without login (section 4, item 4).
- `mcp-auth: 401 has no OAuth discovery header`: `MCP_OAUTH_ISSUER` is empty. Set it, or set `MCP_OAUTH_ENABLED=false` for bearer tokens only.
- `raw-ports: … is reachable`: `BIND_ADDR` isn't `127.0.0.1`. Set it back and run `docker compose up -d`.

**The editor loads but stays "connecting"**
WebSockets are blocked between the browser and Ignis, or `IGNIS_HOST` doesn't match the hostname in the browser exactly (Ignis accepts WebSocket connections only from `https://<IGNIS_HOST>`).

**Agents get `history_pending`**
vault-watcher is down or stalled, and vault-mcp refuses writes it can't record in history. Check `docker compose ps vault-watcher` and its log. If the error says `applied: true`, the write landed and is recorded once the watcher is back.

**Agents get 401**
The token is wrong, expired or revoked (`scripts/token list`). A Claude.ai or ChatGPT connector that stopped working after 30 days without use needs to connect again: refresh tokens expire after 30 days unused.

**git-sync alerts "already has a 'main' branch"**
See section 9: use an empty repository, or set `GIT_ADOPT_REMOTE=true`.

**agent-runner won't start**
`AGENT_RUNNER_TOKEN is not set (run scripts/setup)`: rerun setup after setting `ANTHROPIC_API_KEY` or `AGENT_COMMAND`. A mount error for `skills`: the vault has no `skills/` folder. If the vault has no `AGENTS.md`, run `scripts/setup --adopt`; if it has one, `scripts/upgrade-vault` adds the missing kit files.

**Setup was run with another `--env-file`**
`docker compose`, `scripts/token`, `scripts/backup` and the other scripts read `.env` in the repository root. Use `docker compose --env-file <file> …` for compose commands, or keep the configuration in `.env`.

More on what the kit can't do yet, and on Ignis' behaviour, is in [LIMITATIONS.md](LIMITATIONS.md). Security guidance, including offboarding, is in [SECURITY.md](SECURITY.md).
