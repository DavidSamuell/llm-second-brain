# Docs Vault Kit

An open-source, self-hostable kit for running a shared Obsidian documentation vault where:

- **Humans** edit in the browser (real Obsidian via [Ignis](https://github.com/Nystik-gh/ignis)), behind a login of the deployer's choosing.
- **AI agents** read and write over MCP, following the LLM Wiki pattern (ingest / query / lint). These can be scheduled server-side agents or team members' own agents (Claude Code, Claude.ai, ChatGPT, Cursor, Codex…).
- **Every change is versioned and recoverable**, with or without git. Git backup is optional.

The kit contains no documentation content, only what is needed to deploy, secure, and operate a vault. A deployer brings (or creates) the vault, picks how to publish it, edits `.env`, and runs `docker compose up`.

> **Status:** under development. The specification is in [`SPEC.md`](SPEC.md), and the build plan is in [`tasks/plan.md`](tasks/plan.md). Documents linked below that don't exist yet are added by the tasks that build each part.

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
