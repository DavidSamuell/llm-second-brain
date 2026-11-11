---
description: Check the connection to the team docs vault and walk through sign-in if needed
---
Check that this session can use the team docs vault.

1. Call the vault's `get_schema` tool (from this plugin's `vault` MCP server; its full name ends in
   `vault__get_schema`).
2. If it works, reply in three lines: connected, the vault's `schema_version`, and that the
   `docs-query`, `docs-ingest` and `docs-lint` skills are available. Don't print the schema.
3. If the tool is missing or the server needs authentication, tell the person to sign in:
   - in this session: run `/mcp`, pick the Docs Vault server, choose **Authenticate**, and finish
     the sign-in in the browser (a Google/Microsoft login, or the one-time code from their admin);
   - or in a shell: `claude mcp login <server name shown in /mcp>` (add `--no-browser` over SSH).
   Then run `/docs-vault:connect` again.
4. If the server can't be reached at all, say so and show the address it uses, which comes from
   the plugin option `mcp_url` (`claude plugin configure docs-vault@docs-vault-kit`).
