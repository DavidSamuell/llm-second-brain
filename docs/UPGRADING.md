# Upgrading the Docs Vault Kit

This guide covers three kinds of upgrade, which you can do together or separately:

1. [The kit itself](#1-upgrading-the-kit): new code for vault-mcp, vault-watcher, git-sync, agent-runner, the scripts and the compose files.
2. [Ignis and Obsidian](#2-upgrading-ignis-and-obsidian): the editor image and the Obsidian version it runs. These two pins **always change together**.
3. [The vault template](#3-upgrading-the-vault-template): `AGENTS.md`, page templates, skills and managed `.obsidian` settings inside your vault.

Every upgrade ends with the [smoke test](#4-smoke-test). Run all commands from the kit's folder on the server.

## Before any upgrade

**Read the changelog.** [`CHANGELOG.md`](../CHANGELOG.md) lists new settings, breaking changes and vault template `schema_version` bumps.

**Take a backup**, including the configuration:

```bash
scripts/backup --include-config --out /mnt/backups
```

The archive holds the vault, `DATA_PATH` (history, tokens and OAuth grants, audit log, search index, Ignis state), `.env` and `vault-mcp.yaml`. It is the only way back if an upgrade migrates the token store (see [Token store migrations](#token-store-migrations)). Keep it somewhere safe: it contains secrets. Restoring is described in [SETUP.md, section 12](SETUP.md#12-backups).

**Check for local changes** to tracked files, which `git pull` would refuse to overwrite:

```bash
git status --short
```

Your own settings live in files git ignores (`.env`, `vault-mcp.yaml`, `compose.plugins.yml`, `backups/`), so they are not touched. The one tracked file admins commonly edit is `ignis/ignis-plugins.yaml` (the plugin allowlist). To keep yours out of the way of upgrades, move it outside the repository and point `IGNIS_PLUGINS_FILE` in `.env` at it.

## 1. Upgrading the kit

```bash
git pull
scripts/setup --non-interactive
```

What `scripts/setup --non-interactive` does on an existing installation:

- keeps every value in your `.env`, and adds settings that are new in `.env.example` with their default values;
- rewrites `.env` in the layout of the new `.env.example` (settings that aren't in `.env.example` are kept at the end);
- leaves `vault-mcp.yaml`, the vault content and the existing tokens alone;
- rebuilds the kit's images and restarts what changed with `docker compose up -d --build --wait`;
- runs `scripts/check-exposure` (every profile except `local`).

If you'd rather not rerun setup, the rebuild on its own is:

```bash
docker compose up -d --build --wait
```

Use that only when the changelog lists no new settings: setup is what adds them to `.env` and derives `COMPOSE_FILE` and `COMPOSE_PROFILES`.

After pulling, also compare your policy with the new example and copy over anything you want:

```bash
diff vault-mcp.example.yaml vault-mcp.yaml
docker compose restart vault-mcp vault-watcher      # after editing vault-mcp.yaml
```

The kit's images (`docs-vault-kit/vault-mcp:local`, `…/vault-watcher:local`, `…/git-sync:local`, `…/agent-runner:local`) are built on your server from the checked-out code. No prebuilt images are published yet; when they are, `VAULT_MCP_IMAGE`, `VAULT_WATCHER_IMAGE`, `GIT_SYNC_IMAGE` and `AGENT_RUNNER_IMAGE` in `.env` select them instead of the local builds. Third-party images (Ignis, cloudflared, Caddy, oauth2-proxy) are pinned by digest in the compose files and `.env`; Docker pulls a new pin automatically on the next `up`.

No release has been tagged yet, so upgrading means pulling the branch you cloned. Once releases are tagged, upgrade to a tag instead: `git fetch --tags && git checkout vX.Y.Z`.

### Token store migrations

`DATA_PATH/tokens.db` (bearer tokens, OAuth clients and grants, approval codes) carries a schema version. When a newer vault-mcp opens an older store, it migrates it automatically, in one transaction, the first time it starts (or the first time you run `scripts/token`). There is nothing to run by hand.

A migration only goes forward. If you go back to an older kit after the store was migrated, vault-mcp refuses to start with:

```
error: …/tokens.db: schema v<N> is newer than this vault-mcp
```

To roll back in that case, restore the backup you took before the upgrade.

The search index (`index.db`) is rebuilt from the vault whenever vault-watcher starts, so it needs no migration.

### Rolling back the kit

```bash
git log --oneline -5                  # find the commit you were on
git checkout <previous-commit>
docker compose up -d --build --wait
```

If vault-mcp then reports a newer token store schema, stop the stack and restore the pre-upgrade backup (`docker compose down`, `scripts/restore <archive> --force`, `docker compose up -d`).

## 2. Upgrading Ignis and Obsidian

### The rule: change both pins together

Two settings in `.env` decide which editor runs:

```bash
IGNIS_IMAGE=nobbe/ignis@sha256:2049be7dfbbee5ee9fa5c0ae56efd8555ec57f2ac3275448b1c4318b8af49bb5
OBSIDIAN_VERSION=1.14.4
```

Each Ignis image is built for one Obsidian version (its "pin"). The kit's rule (plan decision D29) is:

- `IGNIS_IMAGE` is pinned **by digest** (`@sha256:…`), never by a tag such as `latest`;
- `OBSIDIAN_VERSION` is set to **exactly** the Obsidian version that digest pins;
- you change **both** in the same edit, never one alone.

Why both:

- An `OBSIDIAN_VERSION` **older** than the image's pin does not fail. Ignis logs `OBSIDIAN_VERSION=<v> is older than this build's Obsidian <pin>; using <pin>.` and runs its own pin. The container looks healthy, but the version you think you pinned is not the one running.
- An `OBSIDIAN_VERSION` **newer** than the image's pin is not guaranteed to work with that Ignis build.

The pinned image in this kit is Ignis 0.9.0 with Obsidian 1.14.4.

Note that `scripts/setup` keeps the values already in your `.env`. When a kit upgrade moves to a new pin pair in `.env.example`, your `.env` keeps the old pair until you copy **both** lines across:

```bash
grep -E '^(IGNIS_IMAGE|OBSIDIAN_VERSION)=' .env.example .env
```

### Finding a new pair

1. Get the digest of the Ignis image you want (replace `latest` with the tag you want):

   ```bash
   docker buildx imagetools inspect nobbe/ignis:latest     # the "Digest:" line is the index digest
   ```

2. Pull that digest and read the Obsidian version it pins, from its label or its environment:

   ```bash
   img=nobbe/ignis@sha256:<digest>
   docker pull "$img"
   docker image inspect --format '{{ index .Config.Labels "com.thiefling.ignis.obsidian-version" }}' "$img"
   docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$img" | grep IGNIS_OBSIDIAN_PIN
   ```

   For the current pin both print `1.14.4`.

3. Put both values in `.env`:

   ```bash
   IGNIS_IMAGE=nobbe/ignis@sha256:<digest>
   OBSIDIAN_VERSION=<version from step 2>
   ```

4. Restart:

   ```bash
   docker compose up -d --wait
   ```

   On the first start after a version change, Ignis downloads the new Obsidian into its cache volume (`obsidian-app`). The health check allows two minutes for this.

### Verify the version that loaded

The Ignis log names the Obsidian version it actually loaded, as `Obsidian ready (v<X>)` after a download or `Obsidian already set up (v<X>)` from the cache:

```bash
docker compose logs ignis | grep -E 'Obsidian (ready|already set up)' | tail -n 1
```

`<X>` must equal `OBSIDIAN_VERSION`. If the log instead says `… is older than this build's Obsidian …`, your `OBSIDIAN_VERSION` doesn't match the image: fix the pair.

### Check the new editor before people use it

A new Ignis release can change behaviour the kit depends on:

- **The exposure check's Ignis markers.** `scripts/check-exposure` recognises Ignis by `apple-mobile-web-app-title" content="Ignis"` or `id="ignis-status"` in its page. If a new version drops both, the check can no longer tell whether the editor is exposed without login. On the server, this must print a number of 1 or more:

  ```bash
  curl -s http://127.0.0.1:8081/ | grep -cE 'apple-mobile-web-app-title" content="Ignis"|id="ignis-status"'
  ```

- **Editing and saving** a note, and the WebSocket connection through your ingress (open the editor at `https://<IGNIS_HOST>`, edit, reload).
- **Allowlisted plugins** still load (if you have any in `ignis/ignis-plugins.yaml`), and installing a plugin from the editor still fails.
- The behaviours recorded in [LIMITATIONS.md](LIMITATIONS.md) (shared layout, concurrent edits, files changed on disk while open) may change; re-check the ones your team relies on.

To roll back, put the previous `IGNIS_IMAGE` and `OBSIDIAN_VERSION` pair back in `.env` and run `docker compose up -d --wait`.

## 3. Upgrading the vault template

A kit upgrade doesn't change files inside your vault. Template changes (`AGENTS.md`, `CLAUDE.md`, page templates, `skills/`, managed `.obsidian` settings) are applied with:

```bash
scripts/upgrade-vault          # shows what would change, then asks before applying
scripts/upgrade-vault --yes    # applies without asking
```

The report starts with `vault schema_version <N> -> kit schema_version <M>` and lists each managed file with one of:

| Report line | Meaning |
|---|---|
| `add: <path>` | The file is missing from the vault and will be added. |
| `update (kit content; your custom blocks are kept): <path>` | The file is unchanged kit content (from any released template version) and will be replaced by the new version. |
| `SKIP: you changed this file; merge by hand or list it in .vault-overrides: <path>` | You edited the file outside custom blocks. It is left alone and the difference is shown, so you can merge by hand. |
| `skip: listed in .vault-overrides: <path>` | The file matches a pattern in `.vault-overrides`. Never touched. |

Only `add` and `update` lines are applied. If there are none, the script prints `nothing to apply`.

### Keeping your own text in kit files: custom blocks

Wrap text you add to a kit-managed file in a custom block:

```markdown
## Citation style

<!-- custom -->
Our team also cites Jira tickets as `[PROJ-123]`.
<!-- /custom -->
```

Text inside `<!-- custom -->` … `<!-- /custom -->` doesn't count as a change to the file, so the file can still be updated. On update, each block is carried into the new version under the same heading. A block whose heading no longer exists in the new version is kept at the end of the file. Both markers are required.

### Opting files out: `.vault-overrides`

To keep your own version of a managed file permanently, list it in `.vault-overrides` at the root of the vault. The syntax is gitignore's: one pattern per line, `/` to anchor to the vault root, a trailing `/` for folders, `**` for any depth, `!` to re-include.

```gitignore
# .vault-overrides
/AGENTS.md
_system/templates/
skills/docs-lint/
```

### Notes

- `upgrade-vault` writes atomically under the vault lock, and vault-watcher records a history version for every changed file, so each change can be undone with `scripts/history restore <path>`.
- It runs in a temporary vault-watcher container; the stack can stay up.
- Ignis's managed settings (`.obsidian/app.json`, `.obsidian/templates.json`) are also reset from the kit's template by `ignis-init` on every start, whether or not you run `upgrade-vault`. Other `.obsidian` files are left alone.
- Agents read the rules through `get_schema`, which returns `AGENTS.md` and its `schema_version`. Agents that cache instructions, and copies of the skills installed on people's machines, should be refreshed after a template upgrade (see [CONNECT-AGENTS.md](CONNECT-AGENTS.md)).

## 4. Smoke test

Run after every upgrade.

**1. Services healthy:**

```bash
docker compose ps                                       # every service healthy
curl -s http://127.0.0.1:8082/health                    # vault-mcp: {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/          # Ignis: 200
docker compose exec vault-watcher python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/health', timeout=3)" && echo "vault-watcher ok"
```

**2. Obsidian version** equals `OBSIDIAN_VERSION` ([section 2](#verify-the-version-that-loaded)):

```bash
grep '^OBSIDIAN_VERSION=' .env
docker compose logs ignis | grep -E 'Obsidian (ready|already set up)' | tail -n 1
```

**3. Exposure** (every profile except `local`; replace the hostnames with yours):

```bash
token=$(scripts/token create --name smoke-test --owner you@example.com --scope read --expires 1h)
CHECK_TOKEN=$token scripts/check-exposure https://docs.example.com https://docs-mcp.example.com
```

Every line must say PASS or SKIP. For `local`, check from another machine that `http://<server-ip>:8081/` and `:8082` don't answer.

**4. A token read over MCP.** On the server, read `Home.md` with the read token (create it as in step 3 if you skipped that step):

```bash
url=http://127.0.0.1:8082/mcp
mcp() { curl -sS -H @<(printf 'Authorization: Bearer %s\n' "$token") -H 'Content-Type: application/json' \
          -H 'Accept: application/json, text/event-stream' "$@" "$url"; }
sid=$(mcp -D - -o /dev/null --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-test","version":"1"}}}' \
      | awk 'tolower($1) == "mcp-session-id:" {print $2}' | tr -d '\r')
mcp -H "Mcp-Session-Id: $sid" -o /dev/null --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'
mcp -H "Mcp-Session-Id: $sid" --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read","arguments":{"path":"Home.md"}}}'
scripts/token revoke smoke-test
```

The last `mcp` call prints a `data:` line containing `"isError":false` and the content and `version` of `Home.md`. Use any other readable page if your vault has no `Home.md`.

**5. Editor and history.** Open the editor, change a line in a test note and save. After a few seconds the change appears as a `web` version:

```bash
scripts/history list <path/of/the/note.md>
```

**6. Optional services**, if enabled: `docker compose logs --tail 20 git-sync` shows a successful push after the next `SYNC_INTERVAL`; `docker compose exec agent-runner run-job lint` finishes without an error alert.

If any step fails, see [SETUP.md, Troubleshooting](SETUP.md#14-troubleshooting), or roll back as described above.
