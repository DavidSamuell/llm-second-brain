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
