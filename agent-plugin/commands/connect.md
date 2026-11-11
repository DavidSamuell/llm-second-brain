---
description: Check the connection to the team docs vault and walk through sign-in if needed
---
Check that this session can use the team docs vault.

1. Call the vault's `get_schema` tool (from this plugin's `vault` MCP server; its full name ends in
   `vault__get_schema` or `vault_get_schema`).
2. If it works, reply in three lines: connected, the vault's `schema_version`, and that the
   `docs-query`, `docs-ingest` and `docs-lint` skills are available. Don't print the schema.
3. If the tool is missing or the server needs authentication, tell the person how to sign in in
   their agent, then to run this check again:
   - Claude Code: `/mcp`, pick the Docs Vault server, **Authenticate**; or in a shell
     `claude mcp login <server name shown in /mcp>` (add `--no-browser` over SSH).
   - Codex: `codex mcp login <server name from codex mcp list>`.
   - omp: `/mcp reauth <server name from /mcp list>`.
   The sign-in page asks for their company login or a one-time code from their admin.
4. If the server can't be reached at all, say so and show the address it uses (in the plugin's
   MCP config); the admin can rebuild the plugin with `scripts/agent-plugin --url …`.
