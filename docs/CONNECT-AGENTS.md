# Connecting AI tools to the vault

This guide is for team members who want their AI tools (Claude Code, Cursor, Codex, Claude.ai,
ChatGPT) to read and update the team docs. The tools talk to the vault's MCP server,
`vault-mcp`. Every call goes through it, so permissions, edit-conflict checks, history and the
audit log apply to agents the same way.

There are three ways to connect:

| Client | How it signs in |
|---|---|
| Claude Code with the **Docs Vault plugin** (easiest) | **OAuth**: you sign in once in the browser; no token to copy |
| Claude Code, Cursor, Codex CLI (tools on your computer) | A personal **bearer token** from your admin |
| Claude.ai, ChatGPT (hosted apps) | **OAuth**: you sign in through a browser page and approve access |

## Test status

Tested by hand against a local stack on 2026-11-10: **Claude Code** (CLI 2.1.285) and **Codex
CLI** (0.167.0), each with a bearer token: `get_schema`, `read`, `append`, `submit_source` and
`claim_source` all succeeded and were audited. The server side of OAuth is covered by automated
tests (registration, sign-in with an approval code, consent, token, refresh, revoke, with the MCP
SDK's own client). Cursor, Claude.ai and ChatGPT are **not yet tested against a live client**;
their snippets follow each client's documentation as of 2026-11-10. If one doesn't work for you,
tell your admin so the guide can be fixed.

The **Claude Code plugin** was tested on 2026-11-11 against a public deployment with Claude Code
2.1.285: installed at user scope, signed in with `claude mcp login … --no-browser` and an approval
code, and `/docs-vault:connect` called `get_schema` under the resulting OAuth grant.

## The server address

| Deployment | MCP URL |
|---|---|
| Public host (Cloudflare, Caddy, or your own proxy) | `https://<MCP_HOST>/mcp`, e.g. `https://docs-mcp.example.com/mcp` |
| `INGRESS=local` (SSH tunnel only) | `http://127.0.0.1:8082/mcp` while `ssh -N -L 8082:127.0.0.1:8082 you@server` runs (see [ingress/local.md](ingress/local.md)) |

Your admin will tell you which one applies. Hosted apps (Claude.ai, ChatGPT) connect from their
providers' cloud, so they only work with a public `https://<MCP_HOST>`, and only when the admin
has OAuth turned on (`MCP_OAUTH_ENABLED=true` with `MCP_OAUTH_ISSUER` set; see
[SETUP.md](SETUP.md)). How tokens and grants are protected and revoked is in
[SECURITY.md](SECURITY.md).

The examples below use `https://docs-mcp.example.com/mcp`; replace it with your URL.

## Claude Code plugin (easiest)

The kit repository is also a Claude Code plugin marketplace. The `docs-vault` plugin adds the
vault's MCP server and the kit's three skills, and you sign in through the browser instead of
pasting a token. Your admin must have OAuth turned on (see above) and gives you the vault's MCP
address.

```bash
claude plugin marketplace add DavidSamuell/llm-second-brain     # once
claude plugin install docs-vault@docs-vault-kit --scope user    # see "Scope" below
```

Claude Code asks for the plugin's one option, **Vault MCP address** (e.g.
`https://docs-mcp.example.com/mcp`). Set or change it later with
`/plugin configure docs-vault@docs-vault-kit`, or from a shell:
`echo '{"mcp_url":"https://docs-mcp.example.com/mcp"}' | claude plugin configure docs-vault@docs-vault-kit --values-stdin`.

Then sign in, either way:

- In a session: `/mcp` → `plugin:docs-vault:vault` → **Authenticate**.
- From a shell: `claude mcp login plugin:docs-vault:vault`. Over SSH, add `--no-browser`: open
  the printed URL on your own computer, sign in, then paste the address your browser ends up on
  (a `localhost` page that fails to load) back into the terminal.

The sign-in page asks for your company login, or for a one-time code from your admin
(`scripts/oauth approve-code --owner you@example.com --scope write`), depending on how the server
is set up. Run `/docs-vault:connect` to check the connection.

**Scope.** Choose where the plugin is turned on:

| You want | Do |
|---|---|
| Every Claude Code session on this computer | `--scope user` |
| Everyone who works in one project repo | `--scope project`, then commit `.claude/settings.json` (each person still installs and signs in) |
| Only you, only in this project | `--scope local` |
| Only one session | Skip the install; start `claude --plugin-dir <kit checkout>/plugins/docs-vault`. Set the address once for this mode with `claude --plugin-dir <same path> plugin configure docs-vault@inline --values-stdin` (same JSON as above) |

Notes:

- The plugin's tools are named `mcp__plugin_docs-vault_vault__<tool>`. The skills work with any
  prefix.
- Claude Code skips a plugin server whose address is already configured by hand. If you added
  the vault with `claude mcp add` before, remove that entry (`claude mcp remove vault -s user`)
  and ask your admin to revoke its token.
- Sign-in is per computer. `claude mcp logout plugin:docs-vault:vault` signs out; your admin can
  also end the grant (`scripts/token revoke`).

## Step 1 (local tools): get a token from your admin

**Admin**, on the server, in the kit folder:

```bash
scripts/token create --name alice-laptop --owner alice@example.com --scope write
```

- `--scope read` or `write`. A read token can search and read but not change anything.
- `--folders wiki/,raw/` optionally limits the token to some folders.
- `--expires` defaults to `90d`; `never` must be given explicitly.
- The token (`vmcp_…`) is printed once. Only a hash is stored, so a lost token can't be shown
  again: create a new one and revoke the old.

Other admin commands: `scripts/token list` shows every token and OAuth grant (never the secret),
and `scripts/token revoke alice-laptop` stops a token immediately.

**You:** keep the token in a password manager or an environment variable. Don't paste it into
the vault, a chat, or a file you commit. The examples below read it from `VAULT_MCP_TOKEN`:

```bash
export VAULT_MCP_TOKEN=vmcp_...   # e.g. in ~/.bashrc or ~/.zshrc
```

## Claude Code

**Tested** with Claude Code 2.1.285 (2026-11-10), using the same server definition (HTTP
transport, `Authorization: Bearer` header) passed as an MCP config file.

```bash
claude mcp add --transport http --scope user vault https://docs-mcp.example.com/mcp \
  --header "Authorization: Bearer $VAULT_MCP_TOKEN"
```

- `--scope user` makes the server available in every project; leave it out to add it to the
  current project only.
- Naming the server `vault` gives tool names like `mcp__vault__read`, which is what the kit's
  skills mention.
- The shell expands `$VAULT_MCP_TOKEN` when you run the command, so the token is stored in
  Claude Code's config. If you rotate it, run `claude mcp remove vault` and add it again.
- Check: start `claude`, run `/mcp`, and ask it to call `get_schema`.

## Cursor

**Not yet tested against a live client.**

Add this to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` in one project:

```json
{
  "mcpServers": {
    "vault": {
      "url": "https://docs-mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:VAULT_MCP_TOKEN}"
      }
    }
  }
}
```

Cursor fills in `${env:VAULT_MCP_TOKEN}` from the environment it was started in, so set the
variable in your shell profile (or system environment) and restart Cursor. Don't commit a
project `.cursor/mcp.json` with a literal token in it.

## Codex CLI

**Tested** with Codex CLI 0.167.0 (2026-11-10). Older builds can fail before reaching the vault:
the Ubuntu snap 0.114 couldn't read the current model list. Update Codex if `codex exec` reports
`failed to refresh available models` or `model ... is not supported`.

Either run:

```bash
codex mcp add vault --url https://docs-mcp.example.com/mcp --bearer-token-env-var VAULT_MCP_TOKEN
```

or add this to `~/.codex/config.toml`:

```toml
[mcp_servers.vault]
url = "https://docs-mcp.example.com/mcp"
bearer_token_env_var = "VAULT_MCP_TOKEN"
```

Codex asks before each MCP tool call. To let it use the vault's tools without asking every time,
add `default_tools_approval_mode = "approve"` under `[mcp_servers.vault]`; the vault itself still
enforces your token's scope, folders and the page rules.

Codex reads the token from `VAULT_MCP_TOKEN` each time it starts; the token itself isn't written
to the config. Check with `codex mcp list`, or `/mcp` inside `codex`. The same config is used by
the Codex IDE extension.

## Claude.ai and ChatGPT (OAuth custom connectors)

You don't need a token for these. You add the vault as a connector, sign in once in a browser
window, and approve what the app may do.

### Before you start: how you will sign in

Your admin chose one of two sign-in methods:

- **Company identity provider** (`OAUTH_UPSTREAM_PROVIDER` is `google`, `microsoft`, `github` or
  `oidc`). You sign in with your work account. Only email domains the admin allowed get in.
- **One-time approval code** (`OAUTH_UPSTREAM_PROVIDER=none`, the default). Ask your admin for a
  code shortly before you connect. **Admin:**

  ```bash
  scripts/oauth approve-code --owner alice@example.com --scope write --ttl 1h
  ```

  It prints a code like `ABCDE-12345`. `--scope` defaults to `read`; `--ttl` is between 1 minute
  and 7 days (default 1 hour). The code works once. Letters are not case-sensitive and the dash is
  optional. After 5 wrong attempts the sign-in is cancelled and you start again from the app.

### The consent page

After signing in you see **Allow access to the docs vault?** with the app's name (chosen by the
app itself, not verified), the site it will send you back to, and what it asked for. Only click
**Allow** if you started the connection yourself just now.

If your sign-in allows editing, you choose:

- **Read and edit pages** (OAuth scopes `vault:read` + `vault:write`), or
- **Read only** (`vault:read`).

With an approval code made with `--scope read`, the page shows **read only** and offers no
choice. You can always grant less than allowed, never more.

The app then gets an access token valid for 1 hour and a refresh token valid for 30 days, renewed
each time it is used. A connection unused for 30 days stops working and you connect again. The
grant appears in `scripts/token list` as `oauth:<app name>-<6 characters>`; to disconnect for
good, ask your admin to run `scripts/token revoke <that name>`, and remove the connector in the
app too.

### Claude.ai

**Not yet tested against a live client.** The OAuth flow was tested with a scripted MCP SDK
client only; the Claude.ai run is an open check.

As described in Anthropic's help article "Get started with custom connectors using remote MCP"
(2026-11):

- **Pro / Max:** Customize → Connectors → **+ Add** → **Add custom connector**.
- **Team / Enterprise:** an organisation Owner first adds it under Organization settings →
  Connectors → Add → Custom → Web. Members then find it under Customize → Connectors and click
  **Connect**.

Fill in:

- Name: e.g. `Docs vault`
- URL: `https://docs-mcp.example.com/mcp`
- Authentication: a sign-in option ("Sign in now" or "Sign in when needed"), not "No sign in".
- OAuth client: **Register automatically**. The kit implements dynamic client registration; it
  has no support for "Use Claude's published identity" (client ID metadata documents).
- Request headers: leave empty. Don't put a bearer token here: on Team and Enterprise plans a
  header credential is shared by everyone who uses the connector.

Then sign in and approve as described above. Turn the connector on per conversation with the
**+** button → Connectors.

### ChatGPT

**Not yet tested against a live client.**

OpenAI's documentation (2026-11) describes adding a custom MCP server from
[chatgpt.com/plugins](https://chatgpt.com/plugins): the **+** button → **Add custom MCP server**,
enter a name, the URL `https://docs-mcp.example.com/mcp` under Connection, choose OAuth for
authentication, and accept the warning. Your ChatGPT plan and workspace settings decide whether
you may add custom MCP servers at all; workspace admins may need to enable it. Menu names change
often; check OpenAI's current help if these don't match.

Sign in and approve as described above.

## Install the kit's skills

The kit ships three skills that teach an agent the vault's procedures:

| Skill | Use it to |
|---|---|
| `docs-ingest` | Turn one source in `raw/` into wiki pages with citations |
| `docs-query` | Answer a question from the vault, citing pages |
| `docs-lint` | Check the wiki for broken links, missing citations and so on, and file review items |

The Claude Code plugin above already includes them. For other setups, they are in the kit
repository under `vault-template/skills/` (the plugin's copy in `plugins/docs-vault/skills/` is
kept identical), and in your vault's `skills/` folder. If your admin customised them, use the
vault's copy. Copy each folder (it contains a `SKILL.md`) into your client's skills directory:

```bash
git clone https://github.com/DavidSamuell/llm-second-brain.git docs-vault-kit

# Claude Code (Cursor also reads this folder)
mkdir -p ~/.claude/skills && cp -r docs-vault-kit/vault-template/skills/docs-* ~/.claude/skills/

# Codex
mkdir -p ~/.agents/skills && cp -r docs-vault-kit/vault-template/skills/docs-* ~/.agents/skills/

# Cursor only
mkdir -p ~/.cursor/skills && cp -r docs-vault-kit/vault-template/skills/docs-* ~/.cursor/skills/
```

Hosted apps (Claude.ai, ChatGPT) have their own skill upload settings; the kit hasn't been tried
there. Without skills an agent still works: the server tells it to call `get_schema` first, which
returns the vault's rulebook (`AGENTS.md`).

## Tools the server offers

| Tool | What it does |
|---|---|
| `get_schema()` | Returns the vault's `AGENTS.md` with its `schema_version`. Agents read it first. |
| `read(path)` | Reads a file. Returns `content` (or `content_base64` for binary files) and its `version` (sha256); pass that version as `base_version` when you write. |
| `list(folder="")` | Lists one folder (`""` is the vault root), with a frontmatter summary per Markdown page. |
| `search(query, folder=None, limit=20)` | Full-text search over title, headings, path, frontmatter and body. Words are matched literally and all must appear; `folder` narrows the search. |
| `write(path, content, base_version)` | Creates a file (`base_version` null) or replaces it (`base_version` = the version you read). |
| `append(path, text)` | Appends text to an existing file, starting on a new line. |
| `replace_section(path, heading, content, base_version)` | Replaces the body under one heading, subsections included, keeping the heading line. `heading` is the text (`Usage`) or the full line (`## Usage`). |
| `insert_after(path, heading, content)` | Inserts content directly below a heading, before the section's existing body. |
| `review_queue_add(page, issue, suggestion)` | Proposes a change instead of editing: adds a task to `_system/review-queue.md`. |
| `move(path, new_path, base_version)` | Renames a page and updates every link to it in one step. Refused, with nothing changed, if a page the credential can't edit links to it. |
| `submit_source(title, origin, content=None, url=None)` | Drops a source into `raw/` for ingest. Pass the content you fetched; a `url` alone is recorded for the ingest agent to fetch. The server never fetches it. |
| `claim_source(path, ttl_minutes=None)` | Claims a source in `raw/` before ingesting it: moves it to `raw/_processing/` with a lease (default 30 minutes, 1 to 1440). |
| `complete_source(path, summary_page)` | Finishes a claimed source: moves it to `raw/_processed/` and releases the lease. |
| `release_source(path)` | Gives a claimed source back, for example after a failed ingest: returns it to `raw/`. |

There is no delete tool. A read token can use `get_schema`, `read`, `list` and `search` only.

What a write token may change is set by the admin's `vault-mcp.yaml`. By default agents may write
in `wiki/`, `raw/` and `_system/`, plus any page marked `maintainer: agent` or `shared`; never a
`maintainer: human` page. Folders in `read.deny` (default `private/`, `hr/`) are invisible to
every tool.

Default limits per credential: 120 requests a minute, 30 writes a minute, 2000 writes a day, and
10 MB per file.

## Errors agents see

Errors come back as JSON: `{"code": "...", "message": "...", "details": {...}}`.

| Code | Meaning | What the agent should do |
|---|---|---|
| `conflict` | The file changed since it was read (`base_version` is stale). | Read it again, re-apply the change to the new text, retry. |
| `forbidden` | Outside the credential's folders or write scope, a `maintainer: human` page, a denied path, or a move blocked by a page it can't edit. | Use `review_queue_add` instead. |
| `not_found` | No such file (`append` doesn't create files; use `write`). | Check the path. |
| `invalid` | Bad argument, e.g. a malformed path, a non-http(s) URL, a source not directly in `raw/`, `ttl_minutes` out of range. | Fix the call. |
| `too_large` | The result would exceed `max_file_mb`. | Split the content. |
| `secret_detected` | The new or changed lines look like a secret. | Write `[secret removed]` instead and add a review item. Don't try to get around it. |
| `rate_limited` | Too many calls. | Wait `details.retry_after_s` seconds. |
| `already_claimed` | Another agent is ingesting this source. | Pick another file. |
| `not_claimed` | The credential doesn't hold the lease (it expired or someone else has it). | Don't complete or release it. |
| `ambiguous_heading` | The heading text matches more than one heading. | Use the full heading line, e.g. `## Usage`. |
| `history_pending` | The history service didn't confirm the version in time. | Check `details.applied`: if true the change was made and must not be repeated. While the history service is down, every write is refused with this code and nothing changes. |

A missing, expired or revoked token gets HTTP 401 before any tool runs.
