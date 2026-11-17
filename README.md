# Docs Vault Kit

An open-source, self-hostable kit for running a shared Obsidian documentation vault where:

- **Humans** edit in the browser (real Obsidian via [Ignis](https://github.com/Nystik-gh/ignis)), behind a login of the deployer's choosing.
- **AI agents** read and write over MCP, following the LLM Wiki pattern (ingest / query / lint). These can be scheduled server-side agents or team members' own agents (Claude Code, Claude.ai, ChatGPT, Cursor, Codex…).
- **Every change is versioned and recoverable**, with or without git. Git backup is optional.

The kit contains no documentation content, only what is needed to deploy, secure, and operate a vault. A deployer brings (or creates) the vault, picks how to publish it, edits `.env`, and runs `docker compose up`.

> **Status:** v0.2.0. Everything in [`SPEC.md`](SPEC.md), including the v0.2 second-brain modules (§18), is built and tested. The `cloudflare` profile, Google sign-in, the agent plugin with session hooks (Claude Code and omp; Codex without automatic capture) and nightly maintenance are verified on a live deployment. The `caddy` profile is built and config-validated but not yet deployed on a public host; remaining manual checks are under Deferred in [`tasks/todo.md`](tasks/todo.md). The build plan is in [`tasks/plan.md`](tasks/plan.md).

## Quick start

```bash
git clone https://github.com/DavidSamuell/llm-second-brain.git docs-vault-kit
cd docs-vault-kit
scripts/setup          # asks for folders and how to publish (start with INGRESS=local), then starts the stack
scripts/token create --name alice-laptop --owner alice@example.com --scope write
```

Then follow [`docs/SETUP.md`](docs/SETUP.md) to publish it and [`docs/CONNECT-AGENTS.md`](docs/CONNECT-AGENTS.md) to connect agents.

## What runs

| Service | Role |
|---|---|
| `ignis` (+ one-shot `ignis-init`) | Obsidian in the browser; plugin allowlist and managed settings applied before each start |
| `vault-mcp` | MCP gateway for agents: bearer tokens and OAuth 2.1, policy, version-checked writes, sources, rate limits, audit |
| `vault-watcher` | Version history for every change, full-text search index, secret scan, ops monitor |
| `git-sync` (optional) | Commits and pushes the vault to `GIT_REMOTE`, keeps conflicts as `.conflict` files |
| `agent-runner` (optional) | Scheduled ingest and lint with Claude Code (or `AGENT_COMMAND`) under a monthly budget |
| `cloudflared` / `caddy` + `oauth2-proxy` | Ingress profiles (`INGRESS=cloudflare` / `caddy`) |

Operator scripts: `scripts/setup`, `token`, `history`, `reindex`, `check-exposure`, `upgrade-vault`, `backup`, `restore`.

## Documentation

| Document | For |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | Installing the kit on a Linux server |
| [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md) | Non-technical staff: editing in the browser, adding sources, the review queue |
| [`docs/ingress/`](docs/ingress/) | Publishing the vault: [`cloudflare.md`](docs/ingress/cloudflare.md), [`caddy.md`](docs/ingress/caddy.md), [`pangolin.md`](docs/ingress/pangolin.md), [`nginx.md`](docs/ingress/nginx.md), [`traefik.md`](docs/ingress/traefik.md), [`tailscale.md`](docs/ingress/tailscale.md), [`headscale.md`](docs/ingress/headscale.md), [`local.md`](docs/ingress/local.md) |
| [`docs/CONNECT-AGENTS.md`](docs/CONNECT-AGENTS.md) | Connecting Claude Code, Cursor, Codex, Claude.ai, and ChatGPT |
| [`docs/IMPORTING.md`](docs/IMPORTING.md) | Bringing in existing docs (Confluence, Notion, Google Docs, repos) via agents |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model, tokens and OAuth, secrets, offboarding checklist |
| [`docs/UPGRADING.md`](docs/UPGRADING.md) | Upgrading the kit, Ignis, and Obsidian |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | Known limitations, including the Ignis behaviour verified during the build |

## License

[MIT](LICENSE). Ignis is AGPL-licensed; the kit only references its published image and never vendors its code.
