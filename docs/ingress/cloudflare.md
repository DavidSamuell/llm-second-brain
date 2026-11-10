# Cloudflare Tunnel + Access (`INGRESS=cloudflare`)

The kit runs `cloudflared` next to the other services. It opens an outbound tunnel to Cloudflare, so the server needs **no inbound ports** and no certificate of its own. Cloudflare terminates HTTPS, and Cloudflare Access asks people to sign in before they reach the editor. The MCP host is routed past Access, because vault-mcp checks every request itself (bearer tokens and OAuth).

What you need:

- A domain on Cloudflare (any plan; the free plan is fine).
- Cloudflare Zero Trust enabled on the account (free for up to **50 users**).
- Two hostnames, e.g. `docs.example.com` (editor) and `docs-mcp.example.com` (agents).

Limits to know:

- **Free Zero Trust plan: 50 users** in Access. Beyond that you need a paid seat plan.
- **Upload size: 100 MB per request** on the Free and Pro plans (Cloudflare's request body limit). Large attachments must be added another way, such as git or the server's file system.
- Cloudflare sees all traffic in clear inside its network (it terminates TLS).

## 1. Create the tunnel

1. Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** → **Cloudflared**. Name it, e.g. `docs-vault`.
2. On the install page, copy the **token** (the long string after `--token`). You don't run the install command; the kit runs `cloudflared` in Docker.
3. **Public hostnames** (add two):

   | Subdomain / domain | Service |
   |---|---|
   | `docs.example.com` | `HTTP` → `ignis:8080` |
   | `docs-mcp.example.com` | `HTTP` → `vault-mcp:8082` |

   `ignis` and `vault-mcp` are the compose service names; `cloudflared` reaches them on the compose network. Don't point the tunnel at `localhost:8081`/`8082`.

WebSockets are on by default for tunnels; leave them on (the editor needs them).

## 2. Protect the editor with Access

Zero Trust dashboard → **Access → Applications → Add an application → Self-hosted**:

- **Application domain:** `docs.example.com`.
- **Session duration:** e.g. 24 hours.
- **Identity providers:** one-time PIN (email codes, nothing to set up) or your Google / Microsoft / GitHub / OIDC provider.
- **Policy:** Action **Allow**, Include **Emails ending in** `@example.com`, or a list of emails or an Access group.

## 3. Let agents reach the MCP host without a browser login

vault-mcp must be reachable without an Access login: agents can't do browser sign-ins, and vault-mcp enforces its own authentication. Add a second application:

- **Self-hosted**, **Application domain:** `docs-mcp.example.com` (the whole host: `/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*`).
- **Policy:** Action **Bypass**, Include **Everyone**.

Also check that no account-wide rule adds a challenge in front of this host. **Bot Fight Mode** and WAF managed challenges in particular break agents; skip them for `docs-mcp.example.com` (Security → WAF → custom rule "Skip" for that hostname).

## 4. Configure the kit

```bash
scripts/setup            # choose INGRESS=cloudflare, enter the hostnames and the tunnel token
```

or in `.env`:

```bash
INGRESS=cloudflare
DOMAIN=example.com
IGNIS_HOST=docs.example.com
MCP_HOST=docs-mcp.example.com
CF_TUNNEL_TOKEN=eyJh…                     # from step 1; keep it secret
MCP_OAUTH_ISSUER=https://docs-mcp.example.com   # setup fills this in
```

then `scripts/setup --non-interactive`. Setup sets `COMPOSE_FILE=docker-compose.yml:ingress/cloudflare/compose.yml`, so plain `docker compose up -d` includes `cloudflared`. `BIND_ADDR` stays `127.0.0.1`: nothing needs to listen on a public interface.

## Requirements checklist (§5.1)

| Requirement | How this profile meets it |
|---|---|
| 1. HTTPS | Cloudflare terminates TLS with its edge certificate for both hostnames. |
| 2. WebSockets on the editor | Tunnels pass WebSockets by default; keep "WebSockets" on (Network settings). |
| 3. Login before the editor | The Access application on `IGNIS_HOST` (step 2). |
| 4. MCP host without interactive login | The Access **Bypass** application on `MCP_HOST` (step 3); no WAF challenge on that host. |
| 5. Raw ports not exposed | `cloudflared` connects outbound; `IGNIS_PORT`/`MCP_PORT` stay bound to `127.0.0.1`. |

## Verify

```bash
token=$(scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1h | grep -o 'vmcp_[A-Za-z0-9_-]*')
CHECK_TOKEN=$token scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
scripts/token revoke exposure-check
```

Every line should say PASS (`raw-ports` may say SKIP on hosts without a public address). Then, from a browser in a private window, open `https://docs.example.com`: you should get the Access login, and after it the editor.

## Troubleshooting

- **`mcp-auth` fails with 302/403:** the MCP host is still behind Access or a WAF challenge (step 3).
- **Editor loads but stays "connecting":** WebSockets are off for the zone, or `IGNIS_HOST` in `.env` doesn't match the hostname (Ignis accepts WebSocket origins from `https://IGNIS_HOST` only).
- **`cloudflared` unhealthy:** check the token (`docker compose logs cloudflared`); a revoked or wrong token gives `Unauthorized`.
