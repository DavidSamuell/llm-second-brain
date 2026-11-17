# Ingress profile: `local`

`INGRESS=local` publishes nothing. Ignis and vault-mcp listen only on the server's loopback interface, and you reach them through an SSH tunnel. Use it to try the kit, or for a single user who already has SSH access to the server. No domain, certificate, or identity provider is needed.

```
your laptop ── ssh -L ──► server 127.0.0.1:8081  (Ignis)
                      └─► server 127.0.0.1:8082  (vault-mcp)
```

Hosted MCP clients (Claude.ai, ChatGPT) can't reach a `local` deployment. To expose it later, pick another profile or recipe in this folder.

## 1. `.env`

```bash
INGRESS=local
BIND_ADDR=127.0.0.1             # required; this is what keeps the services private
IGNIS_PORT=8081
MCP_PORT=8082
IGNIS_HOST=                     # leave unset; there is no public editor host
MCP_HOST=
MCP_OAUTH_ENABLED=false         # no public MCP host, so OAuth connectors can't be used
MCP_OAUTH_ISSUER=
```

Run `scripts/setup` (or `scripts/setup --reconfigure` when switching from another profile), then `docker compose up -d`. `local` adds no compose override (`COMPOSE_FILE` is just the base file), so no proxy or tunnel container runs.

`MCP_OAUTH_ISSUER` stays empty: there is no public MCP origin for OAuth metadata to point at, and vault-mcp accepts bearer tokens only. If you leave `MCP_OAUTH_ENABLED=true` with an empty issuer, vault-mcp logs a warning and runs bearer-only anyway.

## 2. Connect

From your laptop:

```bash
ssh -N -L 8081:127.0.0.1:8081 -L 8082:127.0.0.1:8082 you@server
```

- Editor: open `http://127.0.0.1:8081`. Browsers treat `127.0.0.1` and `localhost` as secure contexts, so Ignis works over plain HTTP on the tunnel.
- MCP: create a token on the server and point your agent at the tunnel:

```bash
# on the server
scripts/token create --name alice-laptop --owner alice@example.com --scope write --expires 90d

# on the laptop, for example Claude Code
claude mcp add --transport http docs-vault http://127.0.0.1:8082/mcp \
  --header "Authorization: Bearer <token>"
```

Agents running on the server itself can use `http://127.0.0.1:8082/mcp` directly, or vault-mcp's stdio mode.

## Requirements checklist (§5.1)

`local` meets the contract by not being an ingress at all: nothing leaves the server except over SSH.

| # | Requirement | How this profile meets it |
|---|---|---|
| 1 | HTTPS | No public endpoint. SSH encrypts the tunnel, and the browser loads Ignis from `http://127.0.0.1`, which counts as a secure context. |
| 2 | WebSockets on the Ignis host | The browser talks to Ignis directly through the SSH port forward, which carries WebSockets unchanged. |
| 3 | User authentication before Ignis | Only someone who can log in over SSH can open the tunnel. SSH login is the authentication. |
| 4 | MCP host without interactive login | vault-mcp is reached at `http://127.0.0.1:8082` through the tunnel with no proxy in between; every request needs a bearer token. There is no public MCP host, so OAuth is off. |
| 5 | Raw ports never exposed | `BIND_ADDR=127.0.0.1` publishes 8081/8082 on loopback only. Don't set `BIND_ADDR=0.0.0.0`: Docker-published ports bypass host firewalls such as ufw. |

## Verify

`scripts/check-exposure` checks public HTTPS URLs, so most of its checks don't apply here. What matters for `local` is that the raw ports are unreachable from other machines. From another machine:

```bash
curl -m 5 http://<server-ip>:8081/      # must fail (connection refused or timeout)
curl -m 5 http://<server-ip>:8082/health  # must fail
```

On the server:

```bash
docker compose ps                       # ports show 127.0.0.1:8081->8080 and 127.0.0.1:8082->8082
curl -s http://127.0.0.1:8082/health    # vault-mcp answers locally
```

Then open the editor through the tunnel, edit a note, and confirm it saves.
