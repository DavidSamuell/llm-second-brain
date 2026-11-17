# Ingress recipe: Traefik + oauth2-proxy

Use this when Traefik v3 is (or will be) your reverse proxy on a server with a public IP and ports 80/443 open. Traefik terminates HTTPS with Let's Encrypt, a `forwardAuth` middleware sends editor requests to oauth2-proxy, and the MCP host goes straight to vault-mcp.

```
docs.example.com      → Traefik (forwardAuth → oauth2-proxy) → 127.0.0.1:8081  (Ignis)
docs-mcp.example.com  → Traefik (no login)                   → 127.0.0.1:8082  (vault-mcp)
```

Traefik runs with `network_mode: host` so that `127.0.0.1:8081`/`8082` are the kit's loopback ports. If your Traefik already runs on a Docker network instead, see [Existing Traefik on a Docker network](#existing-traefik-on-a-docker-network).

## 1. `.env`

```bash
INGRESS=none                    # your own ingress (SPEC §5.2); the kit adds no proxy container
BIND_ADDR=127.0.0.1
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_HOST=docs.example.com     # also sets Ignis's WebSocket Origin allowlist to https://docs.example.com
MCP_HOST=docs-mcp.example.com
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://docs-mcp.example.com
```

Run `scripts/setup --reconfigure` after changing `INGRESS`, then `docker compose up -d`.

`MCP_OAUTH_ISSUER` is the public origin of the MCP host exactly as clients see it: `https://`, no path, no trailing slash. vault-mcp serves its OAuth metadata and endpoints (`/authorize`, `/token`, `/register`, `/revoke`) under it. With an upstream IdP (`OAUTH_UPSTREAM_PROVIDER`), register `https://docs-mcp.example.com/oauth/callback` as its redirect URI. Leave the issuer empty for bearer tokens only (vault-mcp logs a warning; Claude.ai/ChatGPT connectors can't connect). Pick it once: connectors added under an old issuer have to be added again.

## 2. Traefik and oauth2-proxy

Create an OAuth app (Google, Microsoft, GitHub, or any OIDC provider) with redirect URI `https://docs.example.com/oauth2/callback`.

`/opt/traefik/compose.yml`:

```yaml
services:
  traefik:
    image: traefik:v3          # pin a version tag in production
    network_mode: host
    volumes:
      - ./traefik.yml:/etc/traefik/traefik.yml:ro
      - ./dynamic.yml:/etc/traefik/dynamic.yml:ro
      - ./acme:/acme
    restart: unless-stopped

  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy:latest   # pin a version tag in production
    ports:
      - 127.0.0.1:4180:4180
    environment:
      OAUTH2_PROXY_PROVIDER: google                   # google | azure | github | oidc …
      OAUTH2_PROXY_CLIENT_ID: ${OAUTH_CLIENT_ID}
      OAUTH2_PROXY_CLIENT_SECRET: ${OAUTH_CLIENT_SECRET}
      OAUTH2_PROXY_EMAIL_DOMAINS: example.com         # who may log in
      OAUTH2_PROXY_REDIRECT_URL: https://docs.example.com/oauth2/callback
      OAUTH2_PROXY_COOKIE_SECRET: ${COOKIE_SECRET}    # openssl rand -base64 32 | tr -- '+/' '-_'
      OAUTH2_PROXY_COOKIE_SECURE: "true"
      OAUTH2_PROXY_HTTP_ADDRESS: 0.0.0.0:4180
      OAUTH2_PROXY_REVERSE_PROXY: "true"
      OAUTH2_PROXY_UPSTREAMS: static://202
      OAUTH2_PROXY_SKIP_PROVIDER_BUTTON: "true"       # unauthenticated → 302 to the provider
      OAUTH2_PROXY_WHITELIST_DOMAINS: docs.example.com
    restart: unless-stopped
```

Put `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, and `COOKIE_SECRET` in `/opt/traefik/.env` (not the kit's `.env`).

`/opt/traefik/traefik.yml` (static config):

```yaml
entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint: { to: websecure, scheme: https }
  websecure:
    address: ":443"

certificatesResolvers:
  le:
    acme:
      email: ops@example.com
      storage: /acme/acme.json
      httpChallenge: { entryPoint: web }

providers:
  file:
    filename: /etc/traefik/dynamic.yml
    watch: true
```

`/opt/traefik/dynamic.yml` (dynamic config):

```yaml
http:
  routers:
    ignis:
      rule: Host(`docs.example.com`)
      entryPoints: [websecure]
      middlewares: [oauth2-auth]
      service: ignis
      tls: { certResolver: le }

    ignis-oauth2:                      # sign-in and callback pages; longer rule wins
      rule: Host(`docs.example.com`) && PathPrefix(`/oauth2/`)
      entryPoints: [websecure]
      service: oauth2-proxy
      tls: { certResolver: le }

    mcp:                               # whole host, no middleware
      rule: Host(`docs-mcp.example.com`)
      entryPoints: [websecure]
      service: vault-mcp
      tls: { certResolver: le }

  middlewares:
    oauth2-auth:
      forwardAuth:
        address: http://127.0.0.1:4180/
        trustForwardHeader: true

  services:
    ignis:
      loadBalancer:
        servers: [{ url: "http://127.0.0.1:8081" }]   # IGNIS_PORT
    vault-mcp:
      loadBalancer:
        servers: [{ url: "http://127.0.0.1:8082" }]   # MCP_PORT
    oauth2-proxy:
      loadBalancer:
        servers: [{ url: "http://127.0.0.1:4180" }]
```

```bash
cd /opt/traefik && mkdir -p acme && docker compose up -d
```

Traefik proxies WebSocket upgrades without extra settings. Firewall: open only 80 and 443, for example `sudo ufw allow 80,443/tcp`.

### Existing Traefik on a Docker network

If Traefik can't use host networking, attach it to the kit's network (`docs-vault_default`) and point the services at the containers instead of loopback: `http://ignis:8080` and `http://vault-mcp:8082`. The routers and middleware stay the same. Don't change `BIND_ADDR` for this; Traefik reaches the containers over the Docker network.

```yaml
# in Traefik's compose file
services:
  traefik:
    networks: [default, docs-vault]
networks:
  docs-vault:
    external: true
    name: docs-vault_default
```

## Requirements checklist (§5.1)

| # | Requirement | How this recipe meets it |
|---|---|---|
| 1 | HTTPS | All routers use the `websecure` entry point with `tls.certResolver: le`; `web` only redirects to HTTPS. |
| 2 | WebSockets on the Ignis host | Traefik passes `Upgrade` requests through by default; the `ignis` router has no middleware that buffers. `IGNIS_HOST` makes Ignis accept the `https://docs.example.com` Origin. |
| 3 | User authentication before Ignis | The `ignis` router uses `oauth2-auth` (`forwardAuth` to oauth2-proxy); only `OAUTH2_PROXY_EMAIL_DOMAINS` can log in. Unauthenticated requests get a 302 to the provider. |
| 4 | MCP host without interactive login | The `mcp` router matches the whole `docs-mcp.example.com` host and has no middleware, so `/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*` all reach vault-mcp. |
| 5 | Raw ports never exposed | `BIND_ADDR=127.0.0.1` keeps 8081/8082 on loopback; oauth2-proxy is published on `127.0.0.1:4180` only; the firewall opens 80/443 only. Don't set `BIND_ADDR=0.0.0.0`: Docker-published ports bypass ufw. |

## Verify

On the server, with a short-lived read token:

```bash
scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1d
read -rs CHECK_TOKEN && export CHECK_TOKEN    # paste the printed token
scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
unset CHECK_TOKEN
scripts/token revoke exposure-check
```

Each §5.4 check prints PASS, FAIL, or SKIP; the exit code is the number of failures. Quick manual checks:

```bash
curl -sI https://docs.example.com | head -1                                         # 302, not the editor
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://docs-mcp.example.com/mcp    # 401
curl -s https://docs-mcp.example.com/.well-known/oauth-authorization-server          # JSON, issuer = MCP_OAUTH_ISSUER
```

Then log in at `https://docs.example.com`, open a note, and confirm edits save (the WebSocket is working).
