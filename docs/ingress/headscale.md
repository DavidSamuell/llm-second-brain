# Ingress recipe: Headscale

Use this when your team runs its own [Headscale](https://headscale.net/) control server instead of Tailscale's. The editor is reachable only from the tailnet; the MCP host is published through a normal public reverse proxy so hosted clients can reach it.

Headscale differs from Tailscale in two ways that shape this recipe:

- **No Funnel.** Headscale can't publish a node to the internet. A public MCP host needs a public IP and a reverse proxy (or another recipe, such as [Pangolin](pangolin.md), for the MCP host only).
- **Serve HTTPS depends on the version.** Released Headscale versions don't issue node HTTPS certificates, so `tailscale serve --https` doesn't work there; support was added on the development branch in late 2026. This recipe uses Nginx with your own domain and certificate, which works on any version.

```
docs.example.com      (tailnet DNS only) → Nginx on tailnet IP, tailnet sources only → 127.0.0.1:8081  (Ignis)
docs-mcp.example.com  (public DNS)       → Nginx on public IP, no login              → 127.0.0.1:8082  (vault-mcp)
```

Needs: the vault server joined to Headscale (here with tailnet IP `100.64.0.5`), Nginx 1.25.1+ on the server, a public IP with port 443 for the MCP host, and certificates for both names. `docs.example.com` has no public DNS record, so issue its certificate with a DNS-01 challenge (for example `certbot certonly --dns-<provider> -d docs.example.com`).

## 1. Headscale: DNS record and policy

In Headscale's `config.yaml`, give the editor name a tailnet-only record (MagicDNS must be on):

```yaml
dns:
  magic_dns: true
  extra_records:
    - name: "docs.example.com"
      type: "A"
      value: "100.64.0.5"        # the vault server's tailnet IP
```

Policy file (`policy.path` in `config.yaml`). Without a policy, Headscale allows all traffic between nodes, so every tailnet member could reach the editor. User names are written as `name@` on current Headscale versions; check the policy reference for yours.

```json
{
  "groups": {
    "group:docs": ["alice@", "bob@"]
  },
  "tagOwners": {
    "tag:docs-vault": ["alice@"]
  },
  "acls": [
    { "action": "accept", "src": ["group:docs"], "dst": ["tag:docs-vault:443"] }
  ]
}
```

Restart Headscale, then on the vault server: `sudo tailscale up --login-server https://headscale.example.com --advertise-tags=tag:docs-vault`.

## 2. `.env`

```bash
INGRESS=none                    # your own ingress (SPEC §5.2); the kit adds no proxy container
BIND_ADDR=127.0.0.1             # Nginx connects over loopback
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_HOST=docs.example.com     # also sets Ignis's WebSocket Origin allowlist to https://docs.example.com
MCP_HOST=docs-mcp.example.com
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://docs-mcp.example.com
```

Run `scripts/setup --reconfigure` after changing `INGRESS`, then `docker compose up -d`.

`MCP_OAUTH_ISSUER` is the public origin of the MCP host exactly as clients see it: `https://`, no path, no trailing slash. vault-mcp serves its OAuth metadata and endpoints (`/authorize`, `/token`, `/register`, `/revoke`) under it. With an upstream IdP (`OAUTH_UPSTREAM_PROVIDER`), register `https://docs-mcp.example.com/oauth/callback` as its redirect URI. Leave the issuer empty for bearer tokens only (vault-mcp logs a warning; Claude.ai/ChatGPT connectors can't connect). Pick it once: connectors added under an old issuer have to be added again.

**Tailnet-only MCP:** if only agents inside the tailnet need MCP (bearer tokens), skip the public server block, give `docs-mcp.example.com` an `extra_records` entry too, add `tag:docs-vault:443` access for those agents, and set `MCP_OAUTH_ENABLED=false`.

## 3. Nginx

`/etc/nginx/conf.d/docs-vault.conf`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# Editor: reachable only from tailnet addresses. Tailnet login + the ACL is the authentication.
server {
    listen 443 ssl;
    http2 on;
    server_name docs.example.com;

    ssl_certificate     /etc/letsencrypt/live/docs.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/docs.example.com/privkey.pem;

    allow 100.64.0.0/10;          # Headscale IPv4 range (prefixes.v4)
    allow fd7a:115c:a1e0::/48;    # Headscale IPv6 range (prefixes.v6)
    deny  all;

    client_max_body_size 100m;

    location / {
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

# MCP host: public, no browser login. vault-mcp authenticates every request itself.
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name docs-mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/docs-mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/docs-mcp.example.com/privkey.pem;

    client_max_body_size 100m;

    # Whole host: /mcp, /.well-known/*, /authorize, /token, /register, /revoke, /oauth/*, /health.
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

Use the ranges from your Headscale `prefixes` setting if you changed them. Keep both blocks on the same `listen 443`: Nginx picks the block by name, and the `allow`/`deny` rules reject non-tailnet clients that send `Host: docs.example.com` to the public IP.

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo ufw allow 443/tcp           # public MCP host; Tailscale's own UDP port as your setup needs
```

## Requirements checklist (§5.1)

| # | Requirement | How this recipe meets it |
|---|---|---|
| 1 | HTTPS | Nginx terminates TLS for both names; `docs.example.com` uses a DNS-01 certificate because it has no public record. |
| 2 | WebSockets on the Ignis host | `proxy_http_version 1.1`, `Upgrade`/`Connection` headers from the `map`, and 1 h timeouts in the editor block. `IGNIS_HOST` makes Ignis accept the `https://docs.example.com` Origin. |
| 3 | User authentication before Ignis | Only tailnet addresses pass `allow`/`deny`, and the Headscale ACL lets only `group:docs` reach `tag:docs-vault:443`. Headscale login is the authentication. |
| 4 | MCP host without interactive login | The `docs-mcp.example.com` block has no access rules and forwards the whole host, including `/mcp`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*`. |
| 5 | Raw ports never exposed | `BIND_ADDR=127.0.0.1` keeps 8081/8082 on loopback; the firewall opens only 443 (and whatever Tailscale itself needs). Don't set `BIND_ADDR=0.0.0.0`: Docker-published ports bypass ufw. |

## Verify

On the vault server, with a short-lived read token:

```bash
scripts/token create --name exposure-check --owner you@example.com --scope read --expires 1d
read -rs CHECK_TOKEN && export CHECK_TOKEN    # paste the printed token
scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
unset CHECK_TOKEN
scripts/token revoke exposure-check
```

Each §5.4 check prints PASS, FAIL, or SKIP; the exit code is the number of failures. The server is itself in the tailnet, so run there the Ignis check may reach the editor; confirm requirement 3 from a machine outside the tailnet:

```bash
# From a machine NOT in the tailnet:
curl -sk --resolve docs.example.com:443:<public-ip> https://docs.example.com -o /dev/null -w '%{http_code}\n'   # 403
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://docs-mcp.example.com/mcp                             # 401
curl -s https://docs-mcp.example.com/.well-known/oauth-authorization-server                                   # JSON, issuer = MCP_OAUTH_ISSUER
```

Then, from a `group:docs` member's device, open `https://docs.example.com`, edit a note, and confirm it saves (the WebSocket is working).
