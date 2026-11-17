# Ingress recipe: Pangolin

Use this when you run (or want to run) [Pangolin](https://docs.pangolin.net/) on a small VPS with a public IP, and the vault server sits behind NAT or a firewall. The vault server runs Pangolin's site connector (Newt), which makes an outbound tunnel to the VPS. Pangolin terminates HTTPS and puts its own SSO in front of the editor. The MCP host is a second resource with authentication turned off.

```
docs.example.com      → Pangolin (SSO) → tunnel → Newt on vault server → 127.0.0.1:8081  (Ignis)
docs-mcp.example.com  → Pangolin (none) → tunnel → Newt on vault server → 127.0.0.1:8082  (vault-mcp)
```

Needs: a working Pangolin install with a domain (wildcard DNS to the VPS, per Pangolin's self-host guide), and Pangolin users or an IdP for the people who should edit.

## 1. `.env`

```bash
INGRESS=none                    # your own ingress (SPEC §5.2); the kit adds no proxy container
BIND_ADDR=127.0.0.1             # Newt connects over loopback
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_HOST=docs.example.com     # also sets Ignis's WebSocket Origin allowlist to https://docs.example.com
MCP_HOST=docs-mcp.example.com
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://docs-mcp.example.com
```

Run `scripts/setup --reconfigure` after changing `INGRESS`, then `docker compose up -d`.

`MCP_OAUTH_ISSUER` is the public origin of the MCP resource exactly as clients see it: `https://`, no path, no trailing slash. vault-mcp serves its OAuth metadata and endpoints (`/authorize`, `/token`, `/register`, `/revoke`) under it. With an upstream IdP (`OAUTH_UPSTREAM_PROVIDER`), register `https://docs-mcp.example.com/oauth/callback` as its redirect URI. Leave the issuer empty for bearer tokens only (vault-mcp logs a warning; Claude.ai/ChatGPT connectors can't connect). Pick it once: connectors added under an old issuer have to be added again.

## 2. Site connector on the vault server

In the Pangolin dashboard, create a site of type **Newt**. The dashboard shows the ID, secret, and endpoint, and a ready-made command for your Pangolin version. Run it on the vault server with **host networking**, so that `127.0.0.1` inside Newt is the server's loopback:

```yaml
# /opt/newt/compose.yml
services:
  newt:
    image: fosrl/newt            # pin a version tag in production
    network_mode: host
    environment:
      PANGOLIN_ENDPOINT: https://pangolin.example.com
      NEWT_ID: <site id>
      NEWT_SECRET: <site secret>
    restart: unless-stopped
```

If the dashboard shows different variable names for your version, use those. Newt only makes outbound connections; the vault server needs no inbound ports.

## 3. Resources

Apply this blueprint in **Settings → Blueprints** (or with `pangolin apply blueprint --file docs-vault.yaml`). Replace `docs-vault-site` with your site's identifier and adjust the allowed roles/users.

```yaml
public-resources:
  docs-vault-editor:
    name: Docs Vault editor
    mode: http
    full-domain: docs.example.com
    auth:
      sso-enabled: true            # Pangolin login required
      sso-roles: [Member]          # or sso-users: [alice@example.com, …]
    targets:
      - site: docs-vault-site
        hostname: 127.0.0.1
        port: 8081                 # IGNIS_PORT
        method: http

  docs-vault-mcp:
    name: Docs Vault MCP
    mode: http
    full-domain: docs-mcp.example.com
    auth:
      sso-enabled: false           # no browser login: vault-mcp authenticates itself
    targets:
      - site: docs-vault-site
        hostname: 127.0.0.1
        port: 8082                 # MCP_PORT
        method: http
```

In the dashboard, the MCP resource must show as not protected: no SSO, password, PIN code, email whitelist, or rules that deny paths. Pangolin's proxy (Traefik) passes WebSockets through without extra settings.

## Requirements checklist (§5.1)

| # | Requirement | How this recipe meets it |
|---|---|---|
| 1 | HTTPS | Pangolin serves both resources over HTTPS with certificates it obtains for `docs.example.com` and `docs-mcp.example.com`. |
| 2 | WebSockets on the Ignis host | Pangolin's Traefik proxies WebSocket upgrades on `http` resources by default. `IGNIS_HOST` makes Ignis accept the `https://docs.example.com` Origin. |
| 3 | User authentication before Ignis | `docs-vault-editor` has `sso-enabled: true`, limited to `sso-roles`/`sso-users`. |
| 4 | MCP host without interactive login | `docs-vault-mcp` has `sso-enabled: false` and no other auth or rules, so the whole host (`/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*`) reaches vault-mcp. |
| 5 | Raw ports never exposed | `BIND_ADDR=127.0.0.1` keeps 8081/8082 on loopback; Newt reaches them through host networking and connects out to the VPS. The vault server needs no open inbound ports. |

## Verify

On the vault server, with a short-lived read token:

```bash
scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1d
read -rs CHECK_TOKEN && export CHECK_TOKEN    # paste the printed token
scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
unset CHECK_TOKEN
scripts/token revoke exposure-check
```

Each §5.4 check prints PASS, FAIL, or SKIP; the exit code is the number of failures. Quick manual checks:

```bash
curl -sI https://docs.example.com | head -1                                         # redirect to Pangolin login, not the editor
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://docs-mcp.example.com/mcp    # 401
curl -s https://docs-mcp.example.com/.well-known/oauth-authorization-server          # JSON, issuer = MCP_OAUTH_ISSUER
```

Then log in at `https://docs.example.com`, open a note, and confirm edits save (the WebSocket is working).
