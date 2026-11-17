# Ingress recipe: Tailscale Serve + Funnel

Use this when your team already uses Tailscale. The editor is served only inside the tailnet with `tailscale serve`: tailnet membership plus your access policy is the login. The MCP host is published to the internet with `tailscale funnel`, so hosted clients such as Claude.ai and ChatGPT can reach it; vault-mcp authenticates every request itself.

**Plan note:** Tailscale's free Personal plan is for personal, non-commercial use. A team using this for work needs a paid Tailscale plan, or [Headscale](headscale.md).

Both services share the node's MagicDNS name (here `docs-vault.tailnet-name.ts.net`) on different ports. MCP gets 443 so hosted clients see a plain `https://` URL; the editor gets 8443.

```
https://docs-vault.tailnet-name.ts.net:8443  → tailscale serve  (tailnet only)  → 127.0.0.1:8081  (Ignis)
https://docs-vault.tailnet-name.ts.net       → tailscale funnel (public)        → 127.0.0.1:8082  (vault-mcp)
```

Needs: Tailscale on the vault server, MagicDNS and HTTPS certificates enabled in the admin console, and the `funnel` node attribute (below). Certificate names for `*.ts.net` are published in public Certificate Transparency logs.

## 1. Access policy

Tag the server and allow only the docs group to reach it. Merge this into your tailnet policy file; if your policy still has the default allow-all rule, remove or narrow it, otherwise every tailnet member can reach the editor.

```json
{
  "groups": {
    "group:docs": ["alice@example.com", "bob@example.com"]
  },
  "tagOwners": {
    "tag:docs-vault": ["autogroup:admin"]
  },
  "grants": [
    { "src": ["group:docs"], "dst": ["tag:docs-vault"], "ip": ["tcp:443", "tcp:8443"] }
  ],
  "nodeAttrs": [
    { "target": ["tag:docs-vault"], "attr": ["funnel"] }
  ]
}
```

On the server:

```bash
sudo tailscale up --advertise-tags=tag:docs-vault --hostname=docs-vault
```

## 2. `.env`

```bash
INGRESS=none                                        # your own ingress (SPEC §5.2); the kit adds no proxy container
BIND_ADDR=127.0.0.1                                 # tailscaled connects over loopback
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_HOST=docs-vault.tailnet-name.ts.net:8443      # Ignis's WebSocket Origin allowlist becomes https://docs-vault.tailnet-name.ts.net:8443
MCP_HOST=docs-vault.tailnet-name.ts.net
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://docs-vault.tailnet-name.ts.net
```

Run `scripts/setup --reconfigure` after changing `INGRESS`, then `docker compose up -d`.

`MCP_OAUTH_ISSUER` is the public origin of the Funnel URL exactly as clients see it: `https://`, no path, no trailing slash (and no port, because Funnel is on 443). vault-mcp serves its OAuth metadata and endpoints (`/authorize`, `/token`, `/register`, `/revoke`) under it. With an upstream IdP (`OAUTH_UPSTREAM_PROVIDER`), register `https://docs-vault.tailnet-name.ts.net/oauth/callback` as its redirect URI. Leave the issuer empty for bearer tokens only (vault-mcp logs a warning; Claude.ai/ChatGPT connectors can't connect). Pick it once: connectors added under an old issuer have to be added again.

## 3. Serve and Funnel

Tailscale client 1.52 or newer:

```bash
# Editor: tailnet only, HTTPS on 8443. Never use `funnel` for this port.
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8081

# MCP host: public, HTTPS on 443.
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8082

tailscale serve status       # 8443 shows "tailnet only", 443 shows "Funnel on"
```

The configuration persists across restarts. To undo: `sudo tailscale serve --https=8443 off` and `sudo tailscale funnel --https=443 off`.

**Tailnet-only MCP:** if only agents inside the tailnet (Claude Code, Cursor, Codex with bearer tokens) need MCP, use `tailscale serve` instead of `funnel` for 443 and drop the `funnel` node attribute. Claude.ai and ChatGPT connectors can't reach the server then; set `MCP_OAUTH_ENABLED=false`.

## Requirements checklist (§5.1)

| # | Requirement | How this recipe meets it |
|---|---|---|
| 1 | HTTPS | `tailscale serve`/`funnel` terminate TLS with the node's Let's Encrypt certificate for `docs-vault.tailnet-name.ts.net`. |
| 2 | WebSockets on the Ignis host | Serve's HTTP proxy passes WebSocket upgrades through without extra settings. `IGNIS_HOST` (with `:8443`) makes Ignis accept that Origin. |
| 3 | User authentication before Ignis | Port 8443 is served only on the tailnet (`serve`, not `funnel`), and the grant lets only `group:docs` reach it. Tailnet login is the authentication. |
| 4 | MCP host without interactive login | `tailscale funnel` forwards all of 443 to vault-mcp with no Tailscale login, including `/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*`. |
| 5 | Raw ports never exposed | `BIND_ADDR=127.0.0.1` keeps 8081/8082 on loopback; the grant allows only tcp:443 and tcp:8443 to the node; Funnel opens only 443 to the internet. |

## Verify

On the vault server, with a short-lived read token:

```bash
scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1d
read -rs CHECK_TOKEN && export CHECK_TOKEN    # paste the printed token
scripts/check-exposure https://docs-vault.tailnet-name.ts.net:8443 https://docs-vault.tailnet-name.ts.net
unset CHECK_TOKEN
scripts/token revoke exposure-check
```

Each §5.4 check prints PASS, FAIL, or SKIP; the exit code is the number of failures. Run on the server, the "Ignis without credentials" check reaches the editor through the tailnet, because the server itself is a member; confirm requirement 3 from a machine outside the tailnet instead:

```bash
# From a machine NOT in the tailnet:
curl -m 10 -sI https://docs-vault.tailnet-name.ts.net:8443 | head -1                            # no response (times out / fails)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://docs-vault.tailnet-name.ts.net/mcp     # 401
curl -s https://docs-vault.tailnet-name.ts.net/.well-known/oauth-authorization-server           # JSON, issuer = MCP_OAUTH_ISSUER
```

Then, from a `group:docs` member's device, open `https://docs-vault.tailnet-name.ts.net:8443`, edit a note, and confirm it saves (the WebSocket is working).
