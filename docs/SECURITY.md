# Security

This document is for the people who deploy and run a Docs Vault Kit server. It describes what the kit protects, what it leaves to you, and what to do when someone leaves the team.

Related documents:

- [SETUP.md](SETUP.md): installing and choosing an ingress.
- [ingress/](ingress/): one recipe per ingress, each with its own login setup.
- [CONNECT-AGENTS.md](CONNECT-AGENTS.md): how people connect their agents.
- [LIMITATIONS.md](LIMITATIONS.md): known gaps that are not security bugs, such as concurrent edits and unattributed web edits.
- [reviews/2026-11-10-oauth-security-review.md](reviews/2026-11-10-oauth-security-review.md): the review of vault-mcp's OAuth server.

## Reporting a vulnerability

Report security problems privately through **GitHub private vulnerability reporting**. Open the repository's **Security** tab and choose **Report a vulnerability**:

<https://github.com/DavidSamuell/llm-second-brain/security/advisories/new>

There is no security email address. Please don't open a public issue for a vulnerability. Include the kit version (or commit), the ingress profile, and the steps to reproduce.

Security fixes go into the latest minor release (SPEC §15). Dependabot opens weekly update PRs for the Python packages, Dockerfiles, compose files and GitHub Actions (`.github/dependabot.yml`). The Ignis image is excluded from Dependabot on purpose: it is upgraded by hand together with `OBSIDIAN_VERSION` (see UPGRADING.md).

## Threat model

### What we protect

| Asset | Where it lives | Why it matters |
|---|---|---|
| Vault content | `VAULT_PATH` (mounted as `/vault`) | The documentation itself, including folders you keep away from agents (`read.deny`, e.g. `private/`, `hr/`). |
| Version history | `DATA_PATH/history/` | Every version of every vault file, including deleted pages, `read.deny` folders, and any secret that was ever saved. |
| Credential store | `DATA_PATH/tokens.db` | Bearer tokens and OAuth grants (stored as SHA-256 hashes), OAuth client registrations, approval-code hashes. |
| OAuth grants | rows in `tokens.db` | A grant lets a hosted app (Claude.ai, ChatGPT) act as a person for up to 30 days at a time, renewed on every use. |
| Audit log | `DATA_PATH/audit.log` | Who did what over MCP; needed to investigate incidents. |
| Secrets in config | `.env`, `vault-mcp.yaml`, `DATA_PATH/git-sync/ssh/`, `DATA_PATH/caddy/`, `DATA_PATH/ignis/` | Tunnel token, OAuth client secrets, cookie secret, Anthropic API key, the agent-runner token, the git deploy key, TLS keys, Ignis plugin state. |

### Who we consider

| Actor | Trusted with | Main risks |
|---|---|---|
| Team members (browser) | Everything in the vault: Ignis has no per-user permissions. | Mistakes, overwriting each other (LIMITATIONS.md), pasting secrets, changing Ignis server settings (below), keeping access after leaving. |
| Team members' agents (Claude Code, Cursor, Codex) | What their bearer token allows: read or write, optional `--folders`, never `read.deny`. | Leaked tokens, over-broad tokens, prompt injection from sources they read. |
| Hosted connectors (Claude.ai, ChatGPT) | What the OAuth grant allows: read or write, all folders except `read.deny`. | Phishing for consent, stolen refresh tokens, grants that outlive the person. |
| Server-side agent (`agent-runner`) | A write token for all folders, plus web fetching. | Prompt injection through ingested sources (below). |
| Internet attackers | Nothing. They can reach the MCP host (it has no browser login) and the ingress login page. | Guessing credentials, abusing open OAuth client registration, flooding, finding an editor exposed without login. |
| A compromised agent or a prompt-injected model | Whatever its credential allows. | Writing bad content, exfiltrating what it can read, burning budget. |

### Trust boundaries

1. **The editor host (`IGNIS_HOST`).** The ingress login is the only access control. Everything behind it is trusted.
2. **The MCP host (`MCP_HOST`).** Open to the internet without a browser login. vault-mcp checks a credential on every request, except `/health` and the OAuth endpoints, which have to be public.
3. **Content from outside.** Sources submitted to `raw/`, web pages fetched by agents, and pages written by other people are data, not instructions. The vault's `AGENTS.md` tells every agent this (rule 1), but that is a request to the model, not an enforced control.
4. **The server.** Anyone with a shell on it, or with access to Docker, can read everything, including `tokens.db` and `.env`. The kit does not defend against the host.

## Ignis has no login of its own

Ignis, the web editor, has **no authentication and no per-user permissions**. Anyone who can reach the Ignis port can read, change and delete every file in the vault. The only thing standing in front of it is your ingress login: Cloudflare Access, oauth2-proxy, Pangolin SSO, tailnet membership, or SSH for `INGRESS=local`.

Rules:

- **Never publish `IGNIS_PORT` (or `MCP_PORT`).** Keep `BIND_ADDR=127.0.0.1` (the default). Docker-published ports bypass host firewalls such as ufw. vault-mcp and vault-watcher print a warning at startup when `BIND_ADDR` is not a loopback address.
- **Run the exposure check** after any ingress change. `scripts/setup` runs it at the end for public profiles. It fails if the editor answers anonymous visitors (it recognises the Ignis page by its HTML markers):

  ```bash
  token=$(scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1h | grep -o 'vmcp_[A-Za-z0-9_-]*')
  CHECK_TOKEN=$token scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
  scripts/token revoke exposure-check
  ```

  The raw-port probe only tests this host's own public addresses. Also run `nc -zv <server> 8081 8082` from another machine; both must fail.
- **Restrict the login to the people who should edit.** An "any Google account" or `*` domain rule hands the vault to the internet.

### What a logged-in person can do in Ignis

We tested the pinned image (`nobbe/ignis@sha256:2049be7d…`, Ignis 0.9.0) on 2026-11-10 by sending requests straight to its HTTP API. Everyone who passes the ingress login can do the following, because Ignis has no roles:

- **Change Ignis server settings** through `POST /api/settings`. This includes the outbound proxy mode and allowlist, the "direct fetch" hosts, and the "Always trust plugins for this vault" switch. The settings are stored in `DATA_PATH/ignis/server-settings.json`. The values the kit sets through environment variables did not change when we sent new values: `WS_ORIGINS` and the private-address block.
- **Turn on Ignis server plugins** through `POST /api/plugins/<id>/enable`. The image bundles one, **Headless Sync**. According to Ignis' documentation, it runs Obsidian Sync on the server "continuously … in the background, resuming on container restart" once someone signs in with their Obsidian Sync account. That would copy the whole vault, `read.deny` folders included, to that person's Obsidian Sync vault. The copy would keep updating after the person has left. We confirmed that the plugin can be switched on through the API; we did not test a real Sync sign-in. The state is stored in `DATA_PATH/ignis/plugin-config.json`. Ignis itself has no setting that disables server plugins, so the kit adds two guards (below).
- **Make the server fetch public URLs** through `POST /api/proxy`. Obsidian uses this for cross-origin requests. Private and loopback addresses are blocked by default (`169.254.169.254`, `127.0.0.1` and the Docker bridge were refused in our test), and the kit does not set `PROXY_ALLOW_PRIVATE_HOSTS`.

### Ignis server plugins

`IGNIS_SERVER_PLUGINS` in `.env` lists the Ignis server plugins you allow (comma-separated ids, e.g. `headless-sync`). It is empty by default.

- **Reset at start.** When the list is empty, `ignis-init` deletes `DATA_PATH/ignis/plugin-config.json` every time it runs (on every `docker compose up` that starts Ignis), so all server plugins start switched off. When the list is not empty, the file is left alone.
- **Alert while running.** vault-watcher's monitor reads that file every `MONITOR_INTERVAL_S` (60 s by default). When a plugin that isn't in `IGNIS_SERVER_PLUGINS` is enabled for any vault, it sends an `ignis_server_plugin_enabled` alert to `ALERT_WEBHOOK`. Like the other monitor checks, it alerts after 3 failed checks in a row, once per occurrence, and reports when it's resolved.

Between the alert and the next restart, the plugin keeps running. Switch it off at once, from the server (`Docs` is your `VAULT_NAME`):

```bash
curl -s -X POST "http://127.0.0.1:${IGNIS_PORT:-8081}/api/plugins/headless-sync/disable" \
  -H 'Content-Type: application/json' -d '{"vault":"Docs"}'
```

Then find out who set it up, and remember that whatever it already synced is in that Obsidian Sync account.

Treat everyone who can log in to the editor as an Ignis administrator: the guards cover server plugins, not the other settings. To check what is switched on:

```bash
cat "$DATA_PATH/ignis/server-settings.json" "$DATA_PATH/ignis/plugin-config.json"
```

### Requests from other websites

- **WebSocket origin.** Compose sets `WS_ORIGINS=https://${IGNIS_HOST}`. In our test, Ignis accepted the WebSocket handshake from any origin. It then closed the connection with `Origin not allowed` as soon as a client with a foreign origin, or with no origin, subscribed, and before sending any data. The allowed origin received file events.
- **Cross-site writes.** The settings and file-write endpoints only read JSON bodies (a `text/plain` body changed nothing). They send no CORS headers, so a browser won't let another site send them JSON. We tested this with `curl`, not in a browser.
- **No Host-header check.** Ignis served files for any `Host` header. With a cookie-based login (Cloudflare Access, oauth2-proxy) or HTTPS on a tailnet name, this doesn't matter: a request for a foreign hostname never gets past the proxy, or fails the certificate check. With `INGRESS=local`, the editor is plain HTTP on `127.0.0.1` through your SSH tunnel, and a DNS-rebinding page could in principle reach it. We did not exercise this. Keep the tunnel open only while you edit.
- Keep the editor login cookie `SameSite=Lax` or stricter. The kit's oauth2-proxy sets `OAUTH2_PROXY_COOKIE_SAMESITE=lax`. For Cloudflare Access, check the application's cookie settings.

## Plugin allowlist

Obsidian community plugins run as JavaScript with full access to the vault in every browser that loads them. The kit only lets in the plugins you list.

- **The list:** `ignis/ignis-plugins.yaml` (or the file named by `IGNIS_PLUGINS_FILE`). It is empty by default. Each entry pins a GitHub repository, a release tag, and the SHA-256 of every release file (`main.js` and `manifest.json` are required, `styles.css` is optional).
- **At every start**, the one-shot `ignis-init` service (`vault-watcher harden-ignis`):
  1. downloads any missing or changed file from the pinned GitHub release (at most 20 MB per file) and checks its SHA-256. **A checksum mismatch stops Ignis from starting.**
  2. deletes everything else from `DATA_PATH/ignis-plugins/`: plugins that aren't listed, and files that aren't listed inside listed plugins.
  3. rewrites `.obsidian/community-plugins.json` to the allowlist, and resets the kit-managed `.obsidian/app.json` and `.obsidian/templates.json` from the template. Other `.obsidian` files are left alone.
- **Read-only mount:** Ignis sees `DATA_PATH/ignis-plugins` at `.obsidian/plugins` read-only, so installing a plugin from the UI fails (verified in T1, see LIMITATIONS.md). A plugin with `writable_data: true` gets only its own `data.json` mounted read-write, through `compose.plugins.yml`. `ignis-init` refuses to run if that file doesn't match the allowlist.
- **Restricted mode is per browser.** Obsidian stores the "community plugins on/off" choice in each browser's localStorage. Allowlisted plugins only run where the person has switched community plugins on. Ignis 0.9.0 also has a server-side "Always trust plugins for this vault" setting that any logged-in person can change (see above). We have not tested how it behaves.

Before adding a plugin, read its code and its release history. A plugin update is a code change: pin the new version and its hashes the same way.

## Bearer tokens

For Claude Code, Cursor, Codex, scripts and agent-runner.

```bash
scripts/token create --name alice-laptop --owner alice@example.com --scope write \
                     [--folders wiki/,raw/] [--expires 90d|never]
scripts/token list [--json]
scripts/token revoke <name-or-id>
```

- **Format and storage.** A token is `vmcp_` followed by 32 random bytes (base64url). It is printed once. Only its SHA-256 is stored in `tokens.db`, together with an 8-character prefix for display. The file is created with mode `0600`.
- **Scope.** `read` can only use the read tools (`get_schema`, `read`, `list`, `search`). `write` can also use the tools that change files. Every write still has to pass the policy in `vault-mcp.yaml` (`write.allow`, `maintainer: human` pages are read-only).
- **Folders.** `--folders` limits read, list, search and write to those folder prefixes (case-sensitive). `read.deny` folders are never served to any credential. `get_schema` (AGENTS.md) is readable by every credential.
- **Expiry.** The default is `90d`. `never` has to be written out. Expired tokens stop working; `token list` shows them as `expired`.
- **Revocation** takes effect on the next request, over HTTP and in stdio mode (`VAULT_MCP_TOKEN` is checked again on every tool call).
- **Name tokens per person and device** (`alice-laptop`). That keeps the audit log readable and lets you revoke one device at a time.
- The `agent-runner` token is created by `scripts/setup` with `--scope write --expires never`, for all folders, with owner `agent-runner@<DOMAIN>`. It is stored in `.env` as `AGENT_RUNNER_TOKEN`.

## OAuth grants (Claude.ai, ChatGPT and other hosted clients)

OAuth is on when `MCP_OAUTH_ENABLED=true` **and** `MCP_OAUTH_ISSUER` is set. A grant is a row in the same credential store, so `scripts/token list` shows it (kind `oauth`, named `oauth:<app name>-<6 characters>`) and `scripts/token revoke` ends it.

### Lifetimes

| Item | Lifetime |
|---|---|
| Pending sign-in (between `/authorize` and consent) | 15 minutes |
| Authorization code | 5 minutes, single use, bound to the client and the PKCE challenge (S256 only) |
| Access token | 1 hour |
| Refresh token | 30 days, replaced on every use. The grant's expiry moves with it, so a grant used at least once every 30 days never expires. |
| Approval code | 1 hour by default, from 1 minute to 7 days (`--ttl`), single use |

Tokens and codes are 256-bit random values, stored as SHA-256.

### Rotation, reuse detection and revocation

- Every refresh returns a new refresh token and marks the old one as used. **If a used refresh token is presented again**, vault-mcp revokes the whole grant, writes `oauth_refresh_reuse` to the audit log and sends a high-priority alert. This covers two requests that race each other too. Someone else may hold a copy of the token: find out how before reconnecting the app.
- `scripts/token revoke <name-or-id>` ends the grant. Its access and refresh tokens stop working on the next request.
- An app can revoke its own grant through `/revoke` (RFC 7009, public clients need no secret). Either token ends the whole grant, and an `oauth_revoke` line is audited.
- Revoking at the identity provider does **not** end grants that already exist. vault-mcp only asks the identity provider at sign-in. Always revoke the grants as well (see the offboarding checklist).

### Who can sign in

Set by `OAUTH_UPSTREAM_PROVIDER`:

| Provider | Rule enforced by vault-mcp |
|---|---|
| `none` (default) | The person types a one-time **approval code** that an admin created for them: `scripts/oauth approve-code --owner alice@example.com [--scope read\|write] [--ttl 1h]`. The scope defaults to `read`. The code fixes the owner and the most the person may grant. |
| `google` | The ID token must have `email_verified: true`, an email in `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`, **and an `hd` claim equal to the email's domain**. That means a Google Workspace account. A personal Google account registered with a work address is refused. |
| `microsoft` | Single tenant only: `OAUTH_UPSTREAM_TENANT` is a tenant id or domain. `common`, `organizations` and `consumers` are refused at startup. The owner is the `email` claim (or `preferred_username`) and its domain must be allowed. Entra ID sends no `email_verified`, so the claim is trusted as given. |
| `github` | Only the account's **primary, verified** email counts, and its domain must be allowed. GitHub sign-up is disabled on the sign-in page. |
| `oidc` | `OAUTH_UPSTREAM_ISSUER` must be https. `email_verified` must be true and the domain must be allowed. |

All upstream providers check the ID token's signature, issuer, audience, expiry and nonce (the GitHub API is used instead of an ID token), and use PKCE. An upstream provider without `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS` refuses to start. With an upstream provider set, the approval-code form is turned off. **Anyone who signs in upstream may grant `write`.** The consent page pre-selects "Read and edit", and the person can narrow it to read only. OAuth grants are never limited to folders: they reach every folder except `read.deny`.

The consent page shows the app's self-chosen name, labelled "not verified", the host it sends you back to, and the requested access. Tell people to approve only a connection they started themselves a moment earlier.

### Client registration

Registration (`/register`) is open to anyone, as the MCP specification expects. vault-mcp limits it:

- Redirect URIs must be https, `http` on a loopback address, or a private app scheme (such as `cursor://`).
- `javascript:`, `data:`, `file:`, `vbscript:`, `about:`, `blob:`, `search:`, `search-ms:`, `shell:`, `help:`, `jar:` and every `ms-*` scheme are refused, as are fragments and `user@host` URIs.
- At most 1000 clients are kept. A client that never got a grant is deleted after an hour, and the oldest unused client is evicted when the cap is reached.
- At most 1000 sign-ins can be pending at once (20 per client). The PKCE challenge must be exactly 43 characters, and `state` and `resource` at most 512.
- Errors on `/authorize` show an error page instead of redirecting to the app, so the vault's hostname can't be used as an open redirect.

### Known remainders

These came out of the 2026-11-10 OAuth review and were documented rather than changed:

- **Entra guest accounts.** Guest (B2B) users in your tenant pass the tenant check, and their `email` comes from their home organisation. If guests in that domain shouldn't connect, open the vault-mcp app in Entra ID (**Enterprise applications → your app → Properties**), set **Assignment required** to **Yes**, and assign only the users or groups who may connect.
- **GitHub verified emails outlive the mailbox.** A GitHub address stays "verified" after the person loses the mailbox. A former employee whose primary GitHub email is still `@yourcompany.com` can keep signing in, and after you revoke a grant they can **create a new one**. GitHub mode has no organisation-membership check. Prefer approval codes, or an identity provider your organisation controls (Google Workspace, Entra ID, your own OIDC).
- **Approval codes are 50 bits, stored as unsalted SHA-256.** A code is 10 Crockford base32 characters. Online guessing is impractical: 5 tries per sign-in, and an `oauth_code_guessing` alert above 30 failures in 10 minutes. But anyone holding a copy of `tokens.db` (for example from a backup) could brute-force a code that is still valid and sign in as its owner. Outstanding codes can't be listed. `scripts/oauth revoke-codes --owner <email>` cancels all unused codes of one person; otherwise they expire. Keep `--ttl` short, send codes over a private channel, and protect backups.
- **Some secrets in `tokens.db` are not hashed:** the client secrets of confidential OAuth clients (the SDK compares them in plain text) and, for up to 15 minutes, the PKCE verifier and nonce of an upstream sign-in in progress.

## Secrets in the vault

Secrets don't belong in documentation. The kit tries to keep them out, and to tell you when one gets in.

- **Rejected on MCP write.** With `secrets.reject_on_write: true` (the default in `vault-mcp.yaml`), a write that **adds or changes** a line matching a secret rule fails with `secret_detected`. The error names only the rule and the line number, never the secret. Lines that were already in the file are not rescanned, so a full `write` that keeps an existing secret line unchanged is accepted (plan D24).
- **The rules** (`vault-core/src/vault_core/secrets.py`) are high-confidence patterns taken from gitleaks: AWS access key ids, GitHub, GitLab, Slack, Stripe, Anthropic, OpenAI and GCP keys, Slack webhooks, Azure storage connection strings, private-key blocks, JWTs, passwords inside connection URLs, and `password|secret|api_key|token = <value>` assignments. The kit's own tokens are matched too (`vault-mcp-token`: bearer `vmcp_`, OAuth access `vmcpa_` and refresh `vmcpr_` tokens). Obvious placeholders such as `<…>`, `${…}`, `xxxx` and `changeme` are ignored. There are no entropy rules and no in-content allowlist. A secret that doesn't match a rule gets through.
- **Detected after any write.** vault-watcher scans every recorded change to a text file, whoever made it: Ignis, git pull, a shell, or vault-mcp. Each new finding produces:
  1. a `secret_detected` alert to `ALERT_WEBHOOK`;
  2. a `#review/high` item in `_system/review-queue.md`;
  3. an audit line.

  The watcher never edits the page. A finding is reported once per path, rule and fingerprint (a hash, never the value). Not scanned: binary files, `_system/review-queue.md` itself, paths that go through a symlink, and `.git/`, `.trash/` and Obsidian's workspace and cache files.
- **History keeps the old versions.** Removing the secret from the page doesn't remove it from `DATA_PATH/history/`, and the kit has **no purge command**. The daily clean-up deletes versions older than `HISTORY_RETENTION_DAYS` (90), but always keeps the newest `HISTORY_MIN_VERSIONS` (10) per file, so the secret can stay there indefinitely. It is also in every backup taken since, and in the git remote if git-sync is on. **Treat any secret that reached the vault as leaked and rotate it.**

## Rate limits

Set per credential in `vault-mcp.yaml`:

```yaml
rate_limits:
  requests_per_minute: 120   # every MCP tool call
  writes_per_minute: 30      # tools that change files (also count as requests)
  writes_per_day: 2000
```

- A refused call returns `rate_limited` with `retry_after_s`. Refused calls don't count, so retrying doesn't push the window forward.
- A credential refused 5 or more times within 10 minutes triggers a `rate_limit_repeated` alert, at most once per credential per hour.
- Counters live in memory, so a vault-mcp restart resets them, including the daily count.
- **Nothing limits requests per IP address.** The OAuth endpoints (`/register`, `/authorize`, `/token`, `/revoke`, `/oauth/*`) are protected only by the caps listed above. If your ingress can do it, add per-IP rate limits and a request-size limit on the MCP host.

## Audit log

`DATA_PATH/audit.log`, JSON Lines, UTC timestamps.

- **Every MCP tool call**, reads included: time, a unique id, service, tool, result (`ok` or an error code such as `forbidden`, `conflict`, `secret_detected`, `rate_limited`), credential id, credential name, owner, and path. Successful writes also record `old_version` and `new_version`, and their id matches the version in history.
- **OAuth events:** `oauth_consent` (who granted which app what), `oauth_login` refusals (wrong approval code, refused upstream identity), `oauth_refresh_reuse`, `oauth_revoke`.
- **Admin commands** (service `vault-mcp-cli`): `token_create`, `token_revoke`, `oauth_approve_code` (owner, scope, expiry; never the code) and `oauth_revoke_codes`.
- **vault-watcher events:** `secret_scan` findings, and recoveries after a crash.
- **Not logged:** what people do in Ignis (see LIMITATIONS.md).
- The file is created with mode `0640` and **is never rotated** (plan D20). It contains people's email addresses and the paths they touched; apply your own retention.

Activity of one person:

```bash
grep -F '"owner":"alice@example.com"' "$DATA_PATH/audit.log"
```

## Symlinks

vault-mcp, the version history, the search index, the secret scan, backups and restores **ignore every path that goes through a symlink**, even one that points inside the vault. Policy is decided on the path string, so a link such as `wiki/x.md → ../private/y.md` would otherwise give `private/` the permissions of `wiki/`. The real path keeps working. Ignis does follow symlinks, so files reached that way can be edited in the browser but are not versioned, scanned or backed up. Don't use symlinks in the vault.

## Server-side agent (agent-runner)

- agent-runner has **no vault mount**. It works through vault-mcp with its own write token, so policy, `read.deny`, rate limits, the secret check and the audit log all apply to it. The only part of the vault it sees directly is `skills/`, read-only.
- By default it runs Claude Code with only the `mcp__vault__*` tools and `WebFetch` (to fetch sources that were submitted as a URL only). It has no shell and no file tools.
- **Residual risk:** a source with prompt-injected instructions could make the agent call `WebFetch` on an attacker's URL with vault text in it. That can be any text the agent-runner token can read, which is everything except `read.deny`. Mitigations:
  - put sensitive folders in `read.deny`;
  - set `AGENT_MONTHLY_BUDGET`;
  - review the audit log and the agent's output (`DATA_PATH/agent-runner/last-*.json`);
  - or set `AGENT_COMMAND` to a stricter agent. `AGENT_COMMAND` runs with whatever tools that command gives itself.
- Team members' own agents carry the same risk with their own tools. Give them `read` tokens or `--folders` where they don't need more.

## Data at rest

- **Permissions.** `tokens.db` is created `0600` (its WAL and SHM files too), `audit.log` `0640`, backups `0600`, and `scripts/setup` writes `.env` as `0600`. The kit does not change the mode of `DATA_PATH` itself. Make it private to the user that runs the stack (`PUID`):

  ```bash
  chmod 700 "$DATA_PATH"
  ```

- **No encryption at rest.** The vault, history, `tokens.db` and the audit log are plain files. Use disk encryption if you need it.
- **`/data` holds more than tokens.** History contains every version of every file, including `read.deny` folders and old secrets. `DATA_PATH/git-sync/ssh/id_ed25519` is the git deploy key, `DATA_PATH/caddy/` holds Caddy's certificates and ACME account (caddy profile), and `DATA_PATH/ignis/` holds Ignis' settings, server-plugin config and, per Ignis' docs, sync state and tokens.
- **Backups contain all of it.** `scripts/backup` archives the vault and all of `DATA_PATH` (except lock files): `tokens.db`, history, the audit log, the deploy key and Ignis' state. `--include-config` also adds `.env` and `vault-mcp.yaml`, which contain the tunnel token, OAuth client secrets, the cookie secret, the Anthropic API key and the agent-runner token. Archives are written `0600` but **not encrypted**. Encrypt them before they leave the server, and treat anyone who can read a backup as having access to everything in it.
- **Restores** check every file against the archive's manifest before writing anything, and extract with Python's tarfile `data` filter (no absolute paths, no links pointing outside the archive, no device files). Restored `.env` and `vault-mcp.yaml` go to `./restored-config/` for review, never over your files.
- `/data` is never committed by git-sync. The git remote does contain the vault's full history, including anything that was ever saved.

## Offboarding checklist

Do all of it the day someone leaves. Revoking at the identity provider alone is not enough: existing vault-mcp credentials keep working until they are revoked or expire, and an existing editor session keeps working until it ends.

**1. List the person's credentials.**

```bash
scripts/token list
```

Look at the `OWNER` column for every row that is `active`. That includes bearer tokens (`KIND bearer`) and OAuth grants (`KIND oauth`, named `oauth:<app>-<id>`).

**2. Revoke every bearer token and OAuth grant they own.**

```bash
scripts/token revoke alice-laptop
scripts/token revoke oauth:Claude-7k2m9q
```

Or all at once, if `jq` is installed on the server:

```bash
owner=alice@example.com
for id in $(scripts/token list --json | jq -r --arg o "$owner" \
    '.[] | select((.owner | ascii_downcase) == $o and .status == "active") | .id'); do
  scripts/token revoke "$id"
done
scripts/token list   # check: nothing active left for that owner
```

**3. Cancel their unused approval codes** (`OAUTH_UPSTREAM_PROVIDER=none`):

```bash
scripts/oauth revoke-codes --owner alice@example.com
```

A code redeemed before you ran this created a grant, so run `scripts/token list` once more and revoke anything new (step 2).

**4. Remove them from the upstream identity provider used by vault-mcp** (`OAUTH_UPSTREAM_PROVIDER`):

- `google`: suspend or delete their Workspace account.
- `microsoft`: disable the account, or remove it from the app's assigned users. Remove guest accounts from the tenant.
- `oidc`: disable the account at your provider.
- `github`: removing them from your GitHub organisation has **no effect**. vault-mcp only checks that the primary verified email is in an allowed domain. If their GitHub primary email is still a verified company address, they can sign in again and create a new grant. Ask them to change it, or switch to another provider.

**5. Remove them from the editor login** (the ingress):

| Ingress | What to do |
|---|---|
| Cloudflare Access | Remove their email or group from the Access policy. If the policy allows a whole domain, add an **Exclude** rule for their email, or disable their account at the identity provider (with one-time PIN, a mailbox they can still read still lets them in). Then revoke their existing Access sessions in the Zero Trust dashboard: a session otherwise lasts until its session duration ends. |
| Caddy, Nginx or Traefik + oauth2-proxy | The kit only checks the email domain (`OAUTH_ALLOWED_EMAIL_DOMAINS`), so disable their account at the identity provider. An existing oauth2-proxy session stays valid until its cookie expires. To end every session now, set a new `OAUTH_COOKIE_SECRET` in `.env` and run `docker compose up -d oauth2-proxy`. Everyone has to sign in again. |
| Pangolin | Remove them from `sso-users` or `sso-roles` of the editor resource. |
| Tailscale / Headscale | Remove them from `group:docs` (or the tailnet), and remove their devices. |
| `local` | Remove their SSH access to the server. |

**6. Check Ignis for anything they switched on:**

```bash
cat "$DATA_PATH/ignis/server-settings.json" "$DATA_PATH/ignis/plugin-config.json"
```

If `headless-sync` (or another server plugin you haven't allowed in `IGNIS_SERVER_PLUGINS`) lists your vault under `enabledVaults`, turn it off with the `curl` command under [Ignis server plugins](#ignis-server-plugins), and find out who set it up. Also check `ignis/ignis-plugins.yaml` and `vault-mcp.yaml` if they could edit the kit's files.

**7. Rotate shared secrets they could have seen.** If they had a shell on the server, access to `.env`, or a copy of a backup, rotate:

- `CF_TUNNEL_TOKEN`, `OAUTH_CLIENT_SECRET`, `OAUTH_COOKIE_SECRET`, `OAUTH_UPSTREAM_CLIENT_SECRET` and `ANTHROPIC_API_KEY`;
- the git deploy key;
- the agent-runner token:

  ```bash
  scripts/token revoke agent-runner
  scripts/token create --name agent-runner --owner agent-runner@example.com --scope write --expires never
  # put the new token in .env as AGENT_RUNNER_TOKEN, then:
  docker compose up -d agent-runner
  ```

**8. Review their recent activity** in the audit log (`grep -F '"owner":"alice@example.com"' "$DATA_PATH/audit.log"`), and check the review queue for anything they left open.
