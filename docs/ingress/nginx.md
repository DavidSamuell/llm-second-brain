# Ingress recipe: Nginx + oauth2-proxy

Use this when the server already runs Nginx on the host with a public IP and ports 80/443 open. Nginx terminates HTTPS, oauth2-proxy handles the browser login for the editor, and the MCP host goes straight to vault-mcp.

```
docs.example.com      → nginx (auth_request → oauth2-proxy) → 127.0.0.1:8081  (Ignis)
docs-mcp.example.com  → nginx (no login)                    → 127.0.0.1:8082  (vault-mcp)
```

Needs: Nginx 1.25.1 or newer on the host (for `http2 on;`; on older versions use `listen 443 ssl http2;`), two DNS records pointing at the server, TLS certificates for both names (for example `certbot --nginx`), and an OAuth app at Google, Microsoft, GitHub, or any OIDC provider.

## 1. `.env`

```bash
INGRESS=none                    # your own ingress (SPEC §5.2); the kit adds no proxy container
BIND_ADDR=127.0.0.1             # keep it; Nginx connects over loopback
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_HOST=docs.example.com     # also sets Ignis's WebSocket Origin allowlist to https://docs.example.com
MCP_HOST=docs-mcp.example.com
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://docs-mcp.example.com
```

Run `scripts/setup --reconfigure` after changing `INGRESS`, then `docker compose up -d`.

`MCP_OAUTH_ISSUER` is the public origin of the MCP host exactly as clients see it: `https://`, no path, no trailing slash. vault-mcp publishes its OAuth metadata and endpoints (`/authorize`, `/token`, `/register`, `/revoke`) under it. If you use an upstream IdP (`OAUTH_UPSTREAM_PROVIDER`), register `https://docs-mcp.example.com/oauth/callback` as its redirect URI. Leave the issuer empty for bearer tokens only (vault-mcp logs a warning and Claude.ai/ChatGPT connectors can't connect). Pick it once: connectors added under an old issuer have to be added again.

## 2. oauth2-proxy (editor login)

Create the OAuth app with redirect URI `https://docs.example.com/oauth2/callback`. Run oauth2-proxy on loopback, for example with Docker:

```bash
# /opt/oauth2-proxy/oauth2-proxy.env
OAUTH2_PROXY_PROVIDER=google                # google | azure | github | oidc …
OAUTH2_PROXY_CLIENT_ID=...
OAUTH2_PROXY_CLIENT_SECRET=...
OAUTH2_PROXY_EMAIL_DOMAINS=example.com      # who may log in
OAUTH2_PROXY_REDIRECT_URL=https://docs.example.com/oauth2/callback
OAUTH2_PROXY_COOKIE_SECRET=...              # generate: openssl rand -base64 32 | tr -- '+/' '-_'
OAUTH2_PROXY_COOKIE_SECURE=true
OAUTH2_PROXY_HTTP_ADDRESS=0.0.0.0:4180
OAUTH2_PROXY_REVERSE_PROXY=true
OAUTH2_PROXY_UPSTREAMS=static://202
OAUTH2_PROXY_SET_XAUTHREQUEST=true
OAUTH2_PROXY_WHITELIST_DOMAINS=docs.example.com
```

```bash
docker run -d --name oauth2-proxy --restart unless-stopped \
  --env-file /opt/oauth2-proxy/oauth2-proxy.env \
  -p 127.0.0.1:4180:4180 \
  quay.io/oauth2-proxy/oauth2-proxy:latest   # pin a version tag in production
```

## 3. Nginx

`/etc/nginx/conf.d/docs-vault.conf`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    listen [::]:80;
    server_name docs.example.com docs-mcp.example.com;
    return 301 https://$host$request_uri;
}

# Editor: login required, WebSockets on.
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name docs.example.com;

    ssl_certificate     /etc/letsencrypt/live/docs.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/docs.example.com/privkey.pem;

    client_max_body_size 100m;   # attachments uploaded through the editor

    location /oauth2/ {
        proxy_pass       http://127.0.0.1:4180;
        proxy_set_header Host                    $host;
        proxy_set_header X-Real-IP               $remote_addr;
        proxy_set_header X-Forwarded-Proto       $scheme;
        proxy_set_header X-Auth-Request-Redirect $request_uri;
    }

    location = /oauth2/auth {
        internal;
        proxy_pass              http://127.0.0.1:4180;
        proxy_set_header        Host              $host;
        proxy_set_header        X-Real-IP         $remote_addr;
        proxy_set_header        X-Forwarded-Proto $scheme;
        proxy_set_header        X-Forwarded-Uri   $request_uri;
        proxy_set_header        Content-Length    "";
        proxy_pass_request_body off;
    }

    location / {
        auth_request /oauth2/auth;
        error_page 401 =403 /oauth2/sign_in?rd=https://$host$request_uri;

        # Keep the session cookie fresh when oauth2-proxy refreshes it.
        auth_request_set $auth_cookie $upstream_http_set_cookie;
        add_header Set-Cookie $auth_cookie;

        proxy_pass         http://127.0.0.1:8081;   # IGNIS_PORT
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        $connection_upgrade;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 1h;                       # long-lived editor WebSocket
        proxy_send_timeout 1h;
    }
}

# MCP host: no browser login. vault-mcp authenticates every request itself.
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name docs-mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/docs-mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/docs-mcp.example.com/privkey.pem;

    client_max_body_size 100m;

    # The whole host passes through: /mcp, /.well-known/*, /authorize, /token,
    # /register, /revoke, /oauth/*, /health. Do not add auth_request here.
    location / {
        proxy_pass         http://127.0.0.1:8082;   # MCP_PORT
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_buffering    off;                      # streamable HTTP responses
        proxy_read_timeout 1h;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Firewall: open only 80 and 443, for example `sudo ufw allow 80,443/tcp`.

## Requirements checklist (§5.1)

| # | Requirement | How this recipe meets it |
|---|---|---|
| 1 | HTTPS | Both `server` blocks listen on `443 ssl` with Let's Encrypt certificates; port 80 only redirects. |
| 2 | WebSockets on the Ignis host | `proxy_http_version 1.1`, `Upgrade`/`Connection` headers from the `map`, and 1 h timeouts in `location /` of `docs.example.com`. `IGNIS_HOST` makes Ignis accept the `https://docs.example.com` Origin. |
| 3 | User authentication before Ignis | `auth_request /oauth2/auth` on every editor request; oauth2-proxy allows only `OAUTH2_PROXY_EMAIL_DOMAINS`. Unauthenticated requests get the sign-in page. |
| 4 | MCP host without interactive login | The `docs-mcp.example.com` block has no `auth_request`; `location /` forwards everything, including `/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*`. |
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
curl -sI https://docs.example.com | head -1                                         # 302 or 403, not the editor
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://docs-mcp.example.com/mcp    # 401
curl -s https://docs-mcp.example.com/.well-known/oauth-authorization-server          # JSON, issuer = MCP_OAUTH_ISSUER
```

Then log in at `https://docs.example.com`, open a note, and confirm edits save (the WebSocket is working).
