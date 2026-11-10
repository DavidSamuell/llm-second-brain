# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Release notes flag breaking changes and vault template `schema_version` bumps.

## [Unreleased]

### Added

- `vault-core`: vault path rules, `vault-mcp.yaml` policy model with read/write decisions, advisory vault lock, atomic writes with sha256 versions, audit log, webhook alerts, and curated secret rules.
- `vault-mcp`: bearer-token credential store and `vault-mcp token create|list|revoke` (`scripts/token`); MCP server with `get_schema`, `read`, `list` over streamable HTTP (`/mcp`) and stdio, unauthenticated `/health`, audit line per tool call.
- `docker-compose.yml` with Ignis, vault-mcp and vault-watcher; ports bound to `BIND_ADDR`; `vault-mcp.example.yaml`.
- `vault-watcher`: version history for every change (inotify plus periodic scan, debounced web edits, git attribution, crash-safe hand-off of MCP writes), FTS5 search index, heartbeat and `/health`.
- `vault-mcp` `search` tool; `scripts/history` (`list`, `restore [--at]`, `restore-deleted`) and `scripts/reindex`.
- CI: lint (ruff, mypy, shellcheck, hadolint), unit, and end-to-end jobs.
- `docs/LIMITATIONS.md`: Ignis behaviour verified against the pinned image (shared layout, concurrent edits, on-disk changes while a note is open, mobile layout, read-only plugins mount, pinned Obsidian version, check-exposure markers).
- `vault-mcp` write tools: `write`, `append`, `replace_section`, `insert_after`, `review_queue_add`, and `move` (Obsidian-style link updates; refused if a page the agent can't edit links to the file). Every write gets a history version and an audit entry before it returns.
- `vault-mcp` source lifecycle: `submit_source`, `claim_source`, `complete_source`, `release_source`, with leases in `/data/state.db` and a sweeper that returns expired claims to `raw/`.
- Per-credential rate limits from `vault-mcp.yaml` (`rate_limited` error with `retry_after_s`); `rate_limit_repeated` alert.
- `vault-watcher` secret scan of every changed text file: high alert, masked `#review/high` review-queue item, audit entry; the secret is never repeated.
- `vault-watcher` ops monitor: alerts when vault-mcp or Ignis is unhealthy, a heartbeat is stale, or `/vault` or `/data` is over 80 % full. New settings: `MONITOR_INTERVAL_S`, `MONITOR_PEERS`, `MONITOR_HEARTBEATS`. `ALERT_WEBHOOK` now also reaches vault-mcp.
- `vault-mcp` OAuth 2.1 authorization server for hosted MCP clients (Claude.ai, ChatGPT): metadata, dynamic client registration, authorization code + PKCE (S256), rotating refresh tokens with reuse detection, revocation, sign-in with one-time approval codes (`vault-mcp oauth approve-code`), and a consent page. Grants appear in `token list` and end with `token revoke`. New settings: `MCP_OAUTH_ENABLED`, `MCP_OAUTH_ISSUER`.
- OAuth sign-in through Google, Microsoft Entra (single tenant), GitHub (primary verified email) or any OIDC provider, limited to `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`; upstream tokens are discarded. New settings: `OAUTH_UPSTREAM_PROVIDER`, `OAUTH_UPSTREAM_CLIENT_ID`, `OAUTH_UPSTREAM_CLIENT_SECRET`, `OAUTH_UPSTREAM_ALLOWED_EMAIL_DOMAINS`, `OAUTH_UPSTREAM_ISSUER`, `OAUTH_UPSTREAM_TENANT`.

### Security

- Tool paths that go through a symlink are refused, even when the target is inside the vault, and the watcher doesn't record or index them, so an alias can't carry one folder's permissions to another.
