# Connecting AI tools to the vault

This guide is for team members who want their AI tools (Claude Code, Cursor, Codex, Claude.ai,
ChatGPT) to read and update the team docs. The tools talk to the vault's MCP server,
`vault-mcp`. Every call goes through it, so permissions, edit-conflict checks, history and the
audit log apply to agents the same way.

There are three ways to connect:

| Client | How it signs in |
|---|---|
| Claude Code, Codex, omp with the team's **Docs Vault plugin** (main way) | **OAuth**: you sign in once in the browser; no token to copy |
| Claude Code, Cursor, Codex CLI configured by hand | A personal **bearer token** from your admin |
| Claude.ai, ChatGPT (hosted apps) | **OAuth**: you sign in through a browser page and approve access |

## Test status

Tested by hand against a local stack on 2026-11-10: **Claude Code** (CLI 2.1.285) and **Codex
CLI** (0.167.0), each with a bearer token: `get_schema`, `read`, `append`, `submit_source` and
`claim_source` all succeeded and were audited. The server side of OAuth is covered by automated
tests (registration, sign-in with an approval code, consent, token, refresh, revoke, with the MCP
SDK's own client). Cursor, Claude.ai and ChatGPT are **not yet tested against a live client**;
their snippets follow each client's documentation as of 2026-11-10. If one doesn't work for you,
tell your admin so the guide can be fixed.

The **Docs Vault plugin** was tested on 2026-11-11 against a public deployment: in Claude Code
2.1.285 (user scope, signed in with `claude mcp login … --no-browser` and an approval code;
`/docs-vault:connect` called `get_schema` under the resulting OAuth grant), in Codex 0.167.1
(`codex mcp login vault`, file credential store; `codex exec` called `get_schema`) and in omp
(`/mcp reauth`; tools called in a session). Each sign-in is its own audited grant.

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

## The Docs Vault plugin (main way)

Your admin builds a plugin for your vault and publishes it as a git repository. It contains the
vault's MCP address, the kit's skills, the `connect`, `document`, `ingest`, `ask` and `lint`
commands and session hooks (see [Automatic capture in project repos](#automatic-capture-in-project-repos)),
and works in Claude Code, Codex and omp. You install it and sign in through the browser; there is
no token to copy.

**Admin**, once per deployment (and again when `MCP_HOST` or the capture allowlist changes):

```bash
scripts/agent-plugin --capture-repos 'github.com/your-org/*'   # address from MCP_HOST in .env; or --url https://…/mcp
cd agent-plugin-dist && git init && git add -A && git commit -m "docs-vault plugin"
git remote add origin git@github.com:your-org/docs-vault-plugin.git && git push -u origin HEAD
```

Make the repository readable by the team (private is fine). OAuth must be on (see above). Below,
`your-org/docs-vault-plugin` stands for that repository. Leave out `--capture-repos` to ship the
plugin with session capture off; each build writes the allowlist anew, so pass it on every
rebuild.

**You**, in your agent:

| Agent | Install | Sign in |
|---|---|---|
| Claude Code | `claude plugin marketplace add your-org/docs-vault-plugin`, then `claude plugin install docs-vault@docs-vault --scope user` | `/mcp` → `plugin:docs-vault:vault` → **Authenticate**, or `claude mcp login plugin:docs-vault:vault` in a shell |
| Codex | `codex plugin marketplace add your-org/docs-vault-plugin`, then `codex plugin add docs-vault@docs-vault` | `codex mcp login vault` |
| omp | `omp plugin marketplace add your-org/docs-vault-plugin`, then `omp plugin install docs-vault@docs-vault` | `/mcp reauth docs-vault:vault` in a session |

The sign-in page asks for your company login, or for a one-time code from your admin
(`scripts/oauth approve-code --owner you@example.com --scope write`), depending on how the server
is set up. On a remote machine (SSH) the sign-in redirects to a `localhost` address on that
machine: open the link in a browser that runs there or forwards its ports (VS Code and similar
remote editors do), or use `claude mcp login … --no-browser`, which asks you to paste the final
address back. Check the connection with the plugin's `connect` command (`/docs-vault:connect` in
Claude Code).

**Where the access applies.** Your sign-in is saved once per agent, per computer account, and
every session of that agent where the plugin is turned on uses it. The install scope only decides
where the plugin is turned on:

| You want | Claude Code | Codex | omp |
|---|---|---|---|
| Every session | `--scope user` | default (`~/.codex/config.toml`) | `--scope user` (default) |
| One project | `--scope project` (commit `.claude/settings.json`) or `--scope local` (just you) | `[plugins."docs-vault@docs-vault"]` in the project's `.codex/config.toml` | `--scope project` |
| One session | `claude --plugin-dir <clone>/docs-vault` instead of installing | — | `omp --plugin-dir <clone>/docs-vault` instead of installing |

Loading the plugin for one session doesn't make the sign-in temporary. To end access, sign out
(`claude mcp logout plugin:docs-vault:vault`, `codex mcp logout vault`, or `/mcp unauth
docs-vault:vault` in omp), and ask your admin to revoke the grant: each sign-in appears in
`scripts/token list` as its own `oauth:…` entry.

Notes:

- Tool names carry the plugin prefix, e.g. `mcp__plugin_docs-vault_vault__read` in Claude Code.
  The skills work with any prefix.
- **Codex on Linux servers:** Codex keeps MCP sign-ins in the desktop keyring by default. On a
  server over SSH that keyring is usually locked, so `codex mcp login` appears to succeed but
  `codex mcp list` still says `Not logged in`. Add `mcp_oauth_credentials_store = "file"` at the
  top of `~/.codex/config.toml` (before any `[section]`) and sign in again; the sign-in is then
  kept in a file in `~/.codex` readable only by you.
- **Codex asks before each vault tool call.** To let it use the plugin's vault tools without
  asking, add this to `~/.codex/config.toml` (the vault still enforces your access and the page
  rules). It must be in the file: a `-c` override on the command line didn't take effect in
  Codex 0.167.1.

  ```toml
  [plugins."docs-vault@docs-vault".mcp_servers.vault]
  default_tools_approval_mode = "approve"
  ```
- An agent skips the plugin's server when the same address is already configured by hand (seen in
  Claude Code). Remove the hand-made entry (`claude mcp remove vault -s user`) and ask your admin
  to revoke its token.
- Updates: when the admin rebuilds and pushes the plugin, update it with
  `claude plugin marketplace update docs-vault && claude plugin update docs-vault@docs-vault`,
  `codex plugin marketplace upgrade docs-vault` or
  `omp plugin marketplace update docs-vault && omp plugin upgrade docs-vault@docs-vault`, then
  start a new session. Refreshing the marketplace alone leaves the installed copy unchanged.

### Automatic capture in project repos

In **Claude Code and omp**, the plugin's hooks make the agent work with the vault on its own in
the repos the admin listed with `--capture-repos`:

- At session start, and again after compaction or `/clear`, a short block (at most 1,500
  characters) gives the agent the repo id and branch and asks it to call `project_context` first
  and to follow the `docs-capture` skill: each durable finding (a decision, an architecture or
  interface, a pipeline or runbook step, an incident root cause) is ingested into the vault as soon
  as it is settled, under your own sign-in. If the vault has no project for the repo, the agent
  offers once to create one.
- Every 10th prompt adds a one-line reminder to capture anything durable so far. Nothing is
  blocked and nothing runs after the session closes; the nightly maintenance run picks up anything
  left unfinished.

Which repos: an entry is `host/path` (one repo) or `host/path/*` (any repo directly under that
path), matched against the `origin` remote of the git top-level in its normalised form
(`git@github.com:Org/App.git` is `github.com/org/app`; GitHub is case-insensitive, other hosts
keep their path's case). An empty allowlist, the default, turns capture off. The allowlist is a
trigger based on the text of `origin`, not proof that a repo belongs to your team: what the agent
may read or change is still decided by your sign-in and the vault's policy.

Opting out, checked on every prompt (reminders stop at once):

- one repo: a `.docs-vault-ignore` file at the repo root (commit it to opt out for everyone);
- one session: start the agent with `DOCS_VAULT_CAPTURE=off`;
- or ask the agent not to capture in this session.

The hooks are small bash scripts that need `bash`, `git` and `jq`. They read only the repo's
git top-level, `origin` and branch, make no network or model calls, and never hold or read vault
credentials. If anything is missing or fails, they add nothing and the session carries on. The
prompt count is kept per session in a private folder under `$TMPDIR`
(`docs-vault-hooks-<uid>`). Claude Code runs them from the plugin's `hooks/hooks.json`; omp
from `hooks/pre/docs-vault.ts`, which calls the same scripts.

omp subagents have independent session ids and may receive these instructions too; the capture
skill deduplicates findings before writing. Hooks are fail-open guidance, not an authorization
boundary or a guarantee that the model obeys them.

**Codex** doesn't run plugin hooks (Codex CLI 0.167–0.169 ignores them), so automatic capture is
not supported there. The vault tools and skills still work; ask the agent to use `docs-capture`.

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

The kit ships four skills that teach an agent the vault's procedures:

| Skill | Use it to |
|---|---|
| `docs-ingest` | Fully ingest one source into cited human docs and atomic wiki/graph references in the same session |
| `docs-query` | Answer from project context, graph and pages with citations, without writing |
| `docs-lint` | Check/fix structure, graph relationships and human docs citing superseded sources |
| `docs-capture` | Fully ingest each durable finding as it becomes settled during work |

The Docs Vault plugin above already includes them (`scripts/agent-plugin` copies them in). For
other setups, they are in the kit repository under `vault-template/skills/`, and in your vault's
`skills/` folder. If your admin customised them, use the vault's copy. Copy each folder (it
contains a `SKILL.md`) into your client's skills directory:

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
| `project_context(repo=None, project=None)` | Readable project summary/status, one-hop graph, open review items and five recent change-log lines, within 4,000 serialized characters. Accepts canonical, SSH or HTTPS repo forms. Unknown, ambiguous or unreadable projects return `project: null`. |
| `graph_neighbors(node, predicate=None, direction="both", depth=1, limit=50)` | Nodes and edges within one or two hops. `node` accepts a node id, unambiguous alias or page path; direction is `in`, `out` or `both`. |
| `graph_edges(subject=None, predicate=None, object=None, status=None, limit=100)` | Matching typed and implicit edges. `explicit` distinguishes declared claims from navigation links. |
| `graph_path(from_node, to_node, max_depth=4)` | One deterministic shortest directed path through readable nodes; maximum depth is 4. |
| `backlinks(path)` | Readable pages referring to a page through links or graph metadata. |
| `graph_lint(folder=None)` | Read-only diagnostics: invalid metadata, missing evidence, dangling references, orphan nodes, duplicate ids/aliases and contradictions. |
| `write(path, content, base_version)` | Creates a file (`base_version` null) or replaces it (`base_version` = the version you read). |
| `append(path, text)` | Appends text to an existing file, starting on a new line. |
| `replace_section(path, heading, content, base_version)` | Replaces the body under one heading, subsections included, keeping the heading line. `heading` is the text (`Usage`) or the full line (`## Usage`). |
| `insert_after(path, heading, content)` | Inserts content directly below a heading, before the section's existing body. |
| `review_queue_add(page, issue, suggestion)` | Proposes a change instead of editing: adds a task to `_system/review-queue.md`. |
| `move(path, new_path, base_version)` | Renames a page and updates body links and graph references. Refused, with nothing changed, if a page the credential can't edit refers to it. |
| `submit_source(title, origin, content=None, url=None)` | Drops a source into `raw/` for ingest. Pass the content you fetched; a `url` alone is recorded for the ingest agent to fetch. The server never fetches it. |
| `claim_source(path, ttl_minutes=None)` | Claims a source in `raw/` before ingesting it: moves it to `raw/_processing/` with a lease (default 30 minutes, 1 to 1440). |
| `complete_source(path, summary_page)` | Finishes a claimed source: moves it to `raw/_processed/` and releases the lease. |
| `release_source(path)` | Gives a claimed source back, for example after a failed ingest: returns it to `raw/`. |

There is no delete tool. Read tokens may use read/search, `project_context` and all five graph tools.

`project_context` is a compact preview: up to three review items, shortened text and only whole
graph records that fit. Use `read` and graph tools for full detail. If both selectors are given,
they must identify the same project. Manual plugin commands are covered in
[the user guide](USER-GUIDE.md); they finish ingestion in the same session,
not at the nightly maintenance run.

What a write token may change is set by the admin's `vault-mcp.yaml`. The v0.2 example allows writes
in `wiki/`, `raw/`, `_system/`, `projects/`, `specs/`, `pipelines/` and `runbooks/`, plus any page
marked `maintainer: agent` or `shared`. A `maintainer: human` page can be updated too, with a
`summary` argument saying what changed and why (the person gets a review-queue notice); the admin
can keep the previous restriction (`human_pages: read_only`). Existing policy files are not widened automatically. The rule files (`AGENTS.md`, `CLAUDE.md`, `skills/`,
`_system/templates/`, the graph ontology) are never writable. Folders in `read.deny` (default
`private/`, `hr/`) are invisible to every tool.

The write tools (`write`, `append`, `replace_section`, `insert_after`, `move`) take an optional
`summary` (up to 200 characters), recorded in the audit log.

Default limits per credential: 120 requests a minute, 30 writes a minute, 2000 writes a day, and
10 MB per file.

### Declaring graph relationships

Markdown remains canonical; the watcher compiles relationships into `DATA_PATH/graph.sqlite`.
Every page becomes a node. Without an explicit id its id uses its node type and full vault-relative
path without `.md`, for example `component:wiki/components/search`. Set `graph.node_id` for an id
that survives moves. Duplicate ids or aliases are ambiguous, not silently merged.

```yaml
type: pipeline
graph:
  node_id: pipeline:training
  relationships:
    - predicate: produces
      object: component:model
      source: wiki/sources/training-readme
      evidence: "The pipeline writes the trained model to the registry."
      confidence: high
      status: current
```

Create the referenced source-summary page first (`type: source-summary` or `source`).
Typed relationships need a registered predicate, valid endpoint types, a source-summary reference,
evidence, confidence (`high`, `medium`, `low`) and status (`current`, `historical`, `proposed`,
`disputed`, `superseded`). Missing target pages are permitted while authoring; lint reports them.
The server checks metadata structure and provenance type, not whether the quotation is true.

Body wikilinks produce low-confidence `mentions` edges; `sources:`, `raw:`, `project:` and a
project's `repo:` list produce navigation edges. Code-fenced and inline-code links are ignored.
Agents cannot edit `_system/graph/ontology.yaml`; admins may extend it. Invalid ontology updates
raise an alert and keep the last valid vocabulary. Schema-1 vaults without the file use the
built-in starter until upgrade.

Every graph tool filters declaring pages, endpoints and traversal intermediates by read policy
and credential folders before applying limits. Unreadable source pages cause both source and
evidence to be redacted. Unknown and unreadable node lookups both return empty results.
`move` preserves explicit ids and rewrites derived-id and frontmatter references as well as
body links, with the same all-or-nothing permission preflight. Expand YAML anchors used in
affected references before moving; such moves are refused rather than changing unrelated values.

Graph support alone does not widen an existing deployment's write policy. Automatic session
capture requires the generated plugin and a non-empty repo allowlist as described above.

## Errors agents see

Errors come back as JSON: `{"code": "...", "message": "...", "details": {...}}`.

| Code | Meaning | What the agent should do |
|---|---|---|
| `conflict` | The file changed since it was read (`base_version` is stale). | Read it again, re-apply the change to the new text, retry. |
| `forbidden` | Outside the credential's folders or write scope, a protected rule file (`details.reason: protected`), a `maintainer: human` page when the admin set `read_only`, a denied path, or a move blocked by a page it can't edit. | Use `review_queue_add` instead. |
| `recently_edited` | Someone saved the page in the editor moments ago. Nothing was written. | Wait `details.retry_after_s`, read the page again, re-apply the change. |
| `not_found` | No such file (`append` doesn't create files; use `write`). | Check the path. |
| `invalid` | Bad argument, e.g. a malformed path, a non-http(s) URL, a source not directly in `raw/`, `ttl_minutes` out of range, a `summary` over 200 characters, or no `summary` on a `maintainer: human` page. | Fix the call. |
| `too_large` | The result would exceed `max_file_mb`. | Split the content. |
| `secret_detected` | The new or changed lines look like a secret. | Write `[secret removed]` instead and add a review item. Don't try to get around it. |
| `rate_limited` | Too many calls. | Wait `details.retry_after_s` seconds. |
| `already_claimed` | Another agent is ingesting this source. | Pick another file. |
| `not_claimed` | The credential doesn't hold the lease (it expired or someone else has it). | Don't complete or release it. |
| `ambiguous_heading` | The heading text matches more than one heading. | Use the full heading line, e.g. `## Usage`. |
| `history_pending` | The history service didn't confirm the version in time. | Check `details.applied`: if true the change was made and must not be repeated. While the history service is down, every write is refused with this code and nothing changes. |

A missing, expired or revoked token gets HTTP 401 before any tool runs.
