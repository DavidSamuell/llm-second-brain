# Checkpoint D: security review of the vault-mcp OAuth 2.1 authorization server

Scope: `vault-mcp/src/vault_mcp/oauth/{grants,provider,pages,upstream}.py`, `store.py`, `server.py` (auth wiring and `current()`), `cli.py` (`oauth approve-code`), `tests/test_oauth.py`. Checked against plan D16–D18 and impl-notes [T24]/[T25]. I read the SDK sources in `.venv/lib/python3.13/site-packages/{mcp,fastmcp}/server/auth/` (mcp SDK handlers, fastmcp 3.4.11 `OAuthProvider`) and `joserfc` 1.8.0, and ran small read-only Python probes against pydantic, joserfc and `redirect_uri_allowed`. I made no code changes.

How each finding was checked:
- **[SDK]**: confirmed by reading the SDK or fastmcp source.
- **[code]**: confirmed by reading the kit's code.
- **[probe]**: confirmed by running a snippet.
- **[inferred]**: depends on how a third party behaves (an IdP, a browser, an MCP client) and was not exercised.

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 6 |
| Low | 6 |
| Info | 11 |

I found nothing that lets an unauthenticated attacker get a token without a victim taking part. The core protocol is sound: PKCE, code single-use, client binding, exact-match redirect URIs, immediate revocation, server-side scope caps and CSRF binding (see "Checked and sound" at the end). The Medium findings fall into three groups:
- **Availability:** one request flood that fills the disk, and two lockouts.
- **Phishing:** an open redirect, and a consent page that can be made misleading.
- **Identity trust:** the email-domain allowlist accepts identities the organisation no longer controls.

---

## Medium

### M1 [MEDIUM] Unauthenticated disk exhaustion through `/authorize` (unbounded `auth_requests`, multi-MB rows)
- **Location:**
  - `vault-mcp/src/vault_mcp/oauth/provider.py:101-104` (`authorize` → `create_request(... params.model_dump(mode="json"))`)
  - `vault-mcp/src/vault_mcp/oauth/grants.py:127-137` (`create_request`: no row cap, no size cap; expired rows are deleted only when the next request arrives)
  - SDK: `mcp/server/auth/routes.py:113-116` (`/authorize` accepts **GET and POST**, 4 MiB body limit at `transport_security.py:15`); `handlers/authorize.py:149` (`params = await request.form()`); `AuthorizationRequest.state/resource/code_challenge` have no length limit.
- **Description:** Each `/authorize` call by any self-registered client inserts one `auth_requests` row holding the client-supplied `state`, `resource` and `code_challenge` verbatim. Starlette caps each form field at 1 MiB, so one POST can store about 3 MB. Rows live 15 minutes and nothing caps how many there are. Registration is open (DCR) and ingress has no login on the MCP host (D39), so no credential is needed.
- **Impact:** At about 100 requests/s × 3 MB × 900 s, roughly 270 GB of steady-state SQLite data (plus WAL) lands in `/data`. That fills the volume holding `tokens.db`, `audit.log`, `index.db` and the writer spool, so every write and every new sign-in fails. If `/data` shares a disk with `/vault`, history and Obsidian saves fail too. [code][SDK]
- **Proof of concept:**
  ```sh
  CID=$(curl -s https://vault.example/register -H 'content-type: application/json' \
    -d '{"redirect_uris":["https://x.example/cb"],"token_endpoint_auth_method":"none"}' | jq -r .client_id)
  python - <<'EOF'
  import httpx, os
  big = "A"*1_000_000
  with httpx.Client() as c:
      while True:
          c.post("https://vault.example/authorize", data={"client_id": os.environ["CID"], "response_type":"code",
                 "code_challenge": big, "state": big, "resource": big, "redirect_uri":"https://x.example/cb"})
  EOF
  ```
- **Recommendation:** In `VaultOAuthProvider.authorize`, refuse oversized parameters. Also cap pending rows globally and per client in `create_request`:
  ```python
  # provider.py, top of authorize()
  if len(params.code_challenge) != 43 or len(params.state or "") > 512 or len(params.resource or "") > 2048:
      raise AuthorizeError("invalid_request", "authorization request parameters are too long")
  # grants.py create_request(), inside BEGIN IMMEDIATE after deleting expired rows
  if conn.execute("SELECT COUNT(*) FROM auth_requests").fetchone()[0] >= 5000 or \
     conn.execute("SELECT COUNT(*) FROM auth_requests WHERE client_id = ?", (client_id,)).fetchone()[0] >= 50:
      raise AuthorizeError("temporarily_unavailable", "too many pending sign-ins")
  ```
  RFC 7636 §4.2 fixes an S256 challenge at exactly 43 characters. Add per-IP rate limits on `/authorize` and `/register` at the ingress as well.

### M2 [MEDIUM] Open redirect on the vault host through `/authorize` error responses with a self-registered client
- **Location:**
  - SDK `mcp/server/auth/handlers/authorize.py:88-135` (`error_response` redirects whenever client and redirect_uri are valid), `:187-195` (invalid scope → redirect), and validation errors such as a missing `code_challenge` or `response_type=token` (also redirect, after the "last-ditch" client load).
  - The kit does not override the `/authorize` route: `provider.py:198-218` only replaces the metadata route.
- **Description:** DCR is open, so an attacker registers `https://evil.example/...` as a redirect URI. A malformed `/authorize` request then 302s straight to that URI with no sign-in, consent or page in between. RFC 9700 §4.11.2 warns that an AS with open registration becomes an open redirector this way. [SDK]
- **Impact:** A link on the company's trusted vault hostname bounces the victim to an attacker page, for example a fake Google/Microsoft sign-in that harvests credentials, or malware. This defeats link-hostname checks in mail filters and user training.
- **Proof of concept:** Register the client as in M1 with `redirect_uris:["https://evil.example/login"]`, then send:
  `https://vault.example/authorize?client_id=$CID&redirect_uri=https://evil.example/login&response_type=code&code_challenge=x&scope=nope`
  The server answers `302 Location: https://evil.example/login?error=invalid_scope&...`.
- **Recommendation:** Wrap the SDK handler and only let the success redirect into `/oauth/login` through; show an HTML error page for everything else. In `VaultOAuthProvider.get_routes`:
  ```python
  sdk = AuthorizationHandler(self)
  async def authorize(request: Request) -> Response:
      response = await sdk.handle(request)
      loc = response.headers.get("location", "")
      if response.status_code in (302, 303) and not loc.startswith(f"{self.issuer}/oauth/login?req="):
          return error_page("This connection request is not valid. Start again from your app.")
      return response
  # replace the Route whose path == "/authorize" with Route("/authorize", authorize, methods=["GET","POST"])
  ```
  The consent "Deny" redirect is fine: it comes only after the person has signed in.

### M3 [MEDIUM] Consent phishing: the page shows an attacker-chosen app name and a misleading "Sends you back to" host
- **Location:**
  - `vault-mcp/src/vault_mcp/oauth/pages.py:217-219` (`name = client_name`; `redirect_host = urlparse(...).netloc`), `:224` (write pre-selected), `:231-234` (rendering)
  - `vault-mcp/src/vault_mcp/oauth/provider.py:40-51` (`redirect_uri_allowed` accepts userinfo)
- **Description:**
  - `client_name` is whatever the registrant says, for example "Claude".
  - The host shown is `netloc`, which includes userinfo. `https://claude.ai@evil.example/cb` passes `redirect_uri_allowed` (hostname `evil.example`), and the page shows **"Sends you back to claude.ai@evil.example"**. [probe: pydantic keeps the userinfo; `urlparse(...).netloc == 'claude.ai@evil.example'`]
  - The attacker started the authorization request, so it holds the PKCE verifier. Once the victim clicks Allow, the code goes to `evil.example`, and the attacker redeems it at `/token`.
  - With an upstream IdP the victim is often already signed in (one click at Google), so the consent page is the only real check.
- **Impact:** A long-lived write grant (30-day rolling refresh) in the victim's name, with all folders (OAuth grants have `folders = NULL`). Approval-code mode is harder to attack because the victim needs a fresh code from the admin.
- **Proof of concept:**
  1. `POST /register {"client_name":"Claude","redirect_uris":["https://claude.ai@evil.example/cb"],"token_endpoint_auth_method":"none"}`
  2. Send the victim `https://vault.example/authorize?client_id=$CID&response_type=code&code_challenge=<S256(v)>&redirect_uri=https://claude.ai@evil.example/cb`. The victim signs in and clicks Allow ("Read and edit" is pre-checked).
  3. `POST /token grant_type=authorization_code&code=...&code_verifier=v&client_id=$CID&redirect_uri=...` returns the tokens.
- **Recommendation:**
  - Reject userinfo at registration: `if parsed.username is not None or parsed.password is not None: return False`.
  - Display `parsed.hostname` (plus `:port`) for http(s), and the full URI for app schemes.
  - Label the name as self-reported: "<name> (name chosen by the app, not verified)". Show when the client registered.
  - Optionally add an `OAUTH_ALLOWED_REDIRECT_HOSTS` allowlist (e.g. `claude.ai,chatgpt.com`), the way fastmcp's `OAuthProxy` restricts client redirect URIs (`fastmcp/server/auth/redirect_validation.py`), so a self-hosted kit can refuse unknown web clients outright.
  - Consider pre-selecting "Read only".

### M4 [MEDIUM] The email-domain allowlist accepts identities the organisation does not (or no longer) controls
- **Location:** `vault-mcp/src/vault_mcp/oauth/upstream.py:121-124` (OIDC: `email_verified` + domain only), `:166-169` (GitHub: primary verified email + domain only), `:204-205` (Microsoft: `email`/`preferred_username` with no verification).
- **Description:**
  - **Google:** anyone can create a *consumer* Google account on an existing address (e.g. `alice@corp.com`), verify it once, and get `email_verified: true` indefinitely. Google's guidance is to check the `hd` claim to restrict sign-in to a Workspace domain. Without that check, a leaver keeps signing in after their Workspace account is suspended. So does anyone who could read a corp mailbox once, such as a shared alias.
  - **GitHub:** a verified email stays verified after the person loses the mailbox. A former employee whose primary GitHub email is still `@corp.com` keeps access.
  - **Microsoft:** Microsoft documents `email` (and `preferred_username` for some account types) as mutable and not domain-verified. It says to check `xms_edov` or not to authorize on email at all. Guest (B2B) users of the pinned tenant pass the `iss`/`aud` checks, and their `email` comes from outside the tenant.

  Grant ownership and the "who may sign in" policy rest entirely on this one check. [code; provider behaviour is **inferred** from IdP docs: Google "hd" claim (developers.google.com/identity/openid-connect/openid-connect), Microsoft "migrate off email claim" / `xms_edov` (learn.microsoft.com/entra/identity-platform/migrate-off-email-claim-authorization)]
- **Impact:** Write access (upstream sign-ins always get `max_scope="write"`, `pages.py:166`) for people the organisation has de-provisioned or never controlled.
- **Proof of concept (Google):** The ex-employee had previously made a Google account on `bob@corp.com` (the "use your existing email" sign-up path). After offboarding, they connect Claude to the vault, sign in with that consumer account (`email_verified=true`, no `hd`), and are admitted as `bob@corp.com` with write access.
- **Recommendation:**
  - **Google:** require `claims.get("hd", "").lower() in allowed_domains`. Document that `gmail.com` can't be allowlisted this way.
  - **Microsoft:** require `xms_edov is True` when trusting `email`. Use `preferred_username` only for member accounts, and reject guests (`acct == 1` optional claim, or the presence of an `idp` claim).
  - **GitHub:** add an `OAUTH_UPSTREAM_GITHUB_ORG` setting and check membership with `GET /user/memberships/orgs/{org}` (`read:org`), or document plainly that GitHub mode admits anyone who ever verified an address in the domain.

### M5 [MEDIUM] Anyone can lock every approval-code sign-in, including correct codes, indefinitely
- **Location:** `vault-mcp/src/vault_mcp/oauth/grants.py:224-236` (the global failure count is checked **before** the code is looked up), `:28` (30 per 10 minutes).
- **Description:** Thirty wrong codes from anyone, spread over any number of self-created requests, refuse *all* sign-ins for 10 minutes, valid codes included. The attacker needs only a DCR client, an `/authorize` request and the login form (cookie + CSRF from GET `/oauth/login`). [code; behaviour is also asserted by `test_thirty_failed_sign_ins_lock_every_request_for_ten_minutes`]
- **Impact:** A script sending about 30 POSTs every 10 minutes stops anyone from connecting a new app in approval-code mode. Existing grants are unaffected. Nothing is audited or alerted (see L3).
- **Proof of concept:** Loop: register once; then 6 × (`/authorize` → GET `/oauth/login` → 5 × POST `/oauth/login` with `code=AAAAAAAAAA`); sleep 9 minutes.
- **Recommendation:** The 50-bit code space already makes brute force infeasible: even 1,000 guesses per 10 minutes for 7 days gives about 1e6 / 1.1e15 per active code. So the global gate doesn't need to fail closed for everyone. Instead:
  1. Keep the per-request limit of 5.
  2. Throttle per client IP (trusted `X-Forwarded-For` from the ingress).
  3. Raise the global cap to something like 600/10 min and turn it into an alert, not a lockout. If a global lockout stays, at least audit and alert on it.

### M6 [MEDIUM] Registration exhaustion: 1000 junk clients block new connections for 7 days
- **Location:** `vault-mcp/src/vault_mcp/oauth/grants.py:29-30,101-122` (cap 1000; only clients older than 7 days with no tokens, codes or requests are pruned; at the cap, registration is **refused**).
- **Description:** Registration is unauthenticated. 1000 POSTs to `/register` fill the table, and every later registration gets `invalid_client_metadata` until the junk is 7 days old. The attacker re-registers to keep it full, and can stop pruning anyway by keeping an `auth_requests` row open per client. Hosted clients such as Claude.ai register once per connecting user, so new users can't connect. [code]
- **Impact:** Persistent denial of new OAuth connections for the cost of about 1000 requests a week.
- **Proof of concept:** `for i in $(seq 1000); do curl -s -XPOST /register -d '{"redirect_uris":["https://x.example/cb"]}' -H 'content-type: application/json' >/dev/null; done`
- **Recommendation:**
  - Prune clients that never obtained a grant after about 1 hour (`created_at < now-1h` and no `token_hashes` rows; outstanding auth_codes/auth_requests excepted, as today).
  - When at the cap, evict the oldest never-used client instead of refusing.
  - Rate-limit `/register` per IP at the ingress.
  - Cap `client_name` and the other free-text metadata (e.g. 200 characters) in `register_client`. Today each stored client can be up to the 4 MiB body limit, so 1000 clients can reach about 4 GB.

---

## Low

### L1 [LOW] RFC 7009 `/revoke` does nothing for public clients that omit `client_secret`
- **Location:** SDK `mcp/server/auth/handlers/revoke.py:25` (`client_secret: str | None` with **no default**, so pydantic treats it as required) → 400 before any revocation. Already noted in impl-notes "Surprises (T24)". The test passes only because it sends `client_secret=""` (`tests/test_oauth.py:410`).
- **Description:** A public PKCE client (the main use case: `token_endpoint_auth_method: none`) that follows RFC 7009 and omits the field gets 400, and its grant stays live: the refresh token keeps working for 30 days, rolling. [SDK]
- **Impact:** "Disconnect" in an MCP client may not end the grant on the server. If the client's tokens leaked, client-side revocation doesn't help. `token revoke` still works.
- **Recommendation:** Replace the `/revoke` route with a subclass whose request model defaults the field:
  ```python
  class _Req(RevocationRequest):
      client_secret: str | None = None
  class VaultRevocationHandler(RevocationHandler):  # copy handle(), validating with _Req
      ...
  ```
  Then add a test that revokes without `client_secret`.

### L2 [LOW] Refresh reuse detection misses concurrent replays
- **Location:** `vault-mcp/src/vault_mcp/oauth/grants.py:369-392` (the reuse check is in `refresh_info`, outside any transaction), `:394-405` (`rotate`: when the conditional UPDATE matches nothing because `used_at` was just set, it raises `GrantError` **without revoking**).
- **Description:** If an attacker and the legitimate client present the same refresh token at the same moment, both pass `load_refresh_token` (`used_at IS NULL`). One rotates; the other gets `invalid_grant`, and the grant is not revoked. The winner could be the attacker. Detection still fires on the *next* use of the stale token, so the gap is narrow. [code]
- **Recommendation:** In `rotate`, when the UPDATE returns no row, look the token up again in the same `BEGIN IMMEDIATE` transaction. If it exists for this client with `used_at` set, revoke the grant before raising.
  ```python
  if row is None:
      used = conn.execute("SELECT credential_id FROM token_hashes WHERE hash=? AND type='refresh' AND client_id=? AND used_at IS NOT NULL",
                          (_hash(token), client_id)).fetchone()
      if used:
          conn.execute("UPDATE credentials SET revoked_at=? WHERE id=? AND kind='oauth' AND revoked_at IS NULL", (_iso(now), used[0]))
          conn.execute("COMMIT"); raise GrantError("refresh token was already used; the grant is revoked")
      conn.execute("ROLLBACK"); raise GrantError(...)
  ```

### L3 [LOW] Security events are not audited or alerted
- **Location:** Only `oauth_consent` (`pages.py:272-275`) and upstream refusals (`pages.py:161-164`) reach `audit.log`. Nothing is logged for:
  - wrong or used approval codes and the global lockout (`grants.py:224-252`);
  - successful approval-code sign-ins;
  - refresh reuse detection revoking a grant (`grants.py:382-384`, a likely token theft);
  - `/revoke`;
  - `/register`.
- **Impact:** M5, M6 and stolen-refresh-token incidents are invisible to the admin. D20's "every tool call is audited" doesn't cover the auth surface. [code]
- **Recommendation:** Audit these events:
  - `oauth_login` (approval code ok/fail, with client_id, never the code);
  - `oauth_lockout`;
  - `oauth_refresh_reuse` (credential_id, owner);
  - `oauth_revoke`;
  - `oauth_register` (client_id, client_name, redirect hosts).

  Send `AlertSender` alerts for lockout and refresh reuse.

### L4 [LOW] Approval codes can be cracked offline from a leaked `tokens.db`, and outstanding codes can't be listed or revoked
- **Location:** `vault-mcp/src/vault_mcp/oauth/grants.py:212-220` (unsalted `sha256` of a 10-character Crockford code, 2^50); `cli.py:137-153` (create only; codes can last up to 7 days).
- **Description:** Unsalted SHA-256 over 2^50 is roughly 1e15 hashes: hours to a day on a few GPUs. A copy of `tokens.db` (a backup or a misconfigured volume) lets an attacker recover live approval codes inside their TTL and sign in as their owner, with write if issued that way. Separately, a code sent over the wrong channel can't be killed before it expires. [code]
- **Recommendation:**
  - Hash with HMAC-SHA256 under a server key kept outside `tokens.db` (e.g. `/data/oauth.key`, 0600), or lengthen codes to 16 characters (80 bits).
  - Add `vault-mcp oauth approve-code --list` and `--revoke <owner|id>`.

### L5 [LOW] App-scheme redirect URIs accept dangerous OS protocol handlers
- **Location:** `vault-mcp/src/vault_mcp/oauth/provider.py:36,45-51` (a short denylist; any other scheme is accepted).
- **Description:** These all register successfully: `ms-msdt:`, `search-ms:`, `ms-officecmd:`, `x-javascript:` and similar. [probe] After the victim clicks **Allow or Deny** (both redirect, `pages.py:260-262,276`), the browser hands an attacker-built URI to an OS handler. Browsers show an external-protocol prompt, so this is low risk. [inferred browser behaviour]
- **Recommendation:** Accept app schemes only if they are reverse-DNS (contain a `.`, RFC 8252 §7.1) or in a small allowlist of known MCP client schemes (`cursor`, `vscode`, `claude`, ...), configurable by env. Show the full app-scheme URI on the consent page.

### L6 [LOW] Upstream owner emails are not normalised or validated
- **Location:** `vault-mcp/src/vault_mcp/oauth/upstream.py:50-57` (`check_domain` keeps the original case and any characters); `pages.py:166` stores the email as the grant owner without `OWNER_RE` (`store.py:35`); `cli.py:74-99` prints `owner` raw to the terminal.
- **Impact:**
  - `Alice@corp.com` and `alice@corp.com` become different owners, which splits audit attribution.
  - A generic `oidc` IdP that allows control characters in emails could inject terminal escape sequences into `token list` output.

  [code]
- **Recommendation:** Lower-case the domain (and the local part for known providers), then reject anything that fails `OWNER_RE` or contains characters outside printable ASCII/Unicode letters before `identify()`.

---

## Info

- **I1 Multiple `aud` values are accepted without an `azp` check** (`upstream.py:109-116`). [probe: joserfc accepts `aud: ["other", client_id]`] Not exploitable: the ID token comes over TLS straight from the token endpoint, in exchange for our code, client secret and verifier. OIDC Core §3.1.3.7 still asks for `azp == client_id` when there are several audiences.
- **I2 A Microsoft tenant given as a domain can never sign in** (`upstream.py:199-205`). [probe: live discovery for `login.microsoftonline.com/microsoft.com/v2.0` reports `issuer` `https://login.microsoftonline.com/72f988bf-…/v2.0`] The exact issuer comparison at `upstream.py:78` therefore fails closed, and the user sees "sign-in service is not reachable". The settings text promises "tenant id or domain". Fix: accept GUIDs only, or resolve the domain to the GUID at start-up.
- **I3 Non-JSON upstream responses raise an uncaught `ValueError`** (`upstream.py:77,103-105,152,166`) and return a 500 instead of the friendly refusal. The pending request is also not dropped. Catch `ValueError` with `httpx.HTTPError`.
- **I4 Reusing an authorization code doesn't revoke the tokens from the first redemption** (OAuth 2.1 §4.1.2 SHOULD). A wrong PKCE verifier also doesn't burn the code (`token.py:178` checks before `exchange`). The verifier space makes guessing pointless, so this is defense in depth only. [SDK]
- **I5 A narrower `scope` on refresh is ignored** (`provider.py:149-161`): the full grant scope is re-issued. This can't escalate: the SDK rejects scopes outside the grant (`token.py:219-229`) and tools check the credential.
- **I6 A missing `scope` means `write`, even for clients registered as read-only** (`grants.py:40-41`; SDK `validate_scope(None)` returns `None`, `shared/auth.py:92-94`). Consent still caps and shows the choice, so this is not attacker-useful. Defaulting to the client's registered `scope` would be more faithful.
- **I7 The authorization response has no RFC 9207 `iss` parameter** (`pages.py:262,276`). Clients that use several authorization servers can't defend against mix-up attacks. Add `iss=self.provider.issuer` and advertise `authorization_response_iss_parameter_supported`.
- **I8 Two secrets are stored in plaintext**, against the `grants.py:1-7` docstring ("every secret … sha256 only"):
  - `upstream_verifier` and `upstream_nonce` (`grants.py:186-193`; short-lived);
  - DCR `client_secret` (`provider.py:88`; documented in [T24], and needed for the SDK's plaintext comparison).

  Fix the docstring, or hash the verifier/nonce and keep the plaintext only in memory for the redirect.
- **I9 Body limits:** fastmcp swaps in its own `/token` route without the SDK's `_body_limited` wrapper (`fastmcp/server/auth/auth.py:767-777`). The kit's `/oauth/login` and `/oauth/consent` POSTs have no body limit either, only Starlette's 1 MiB per field and 1000 fields. [SDK] Set a request-size limit at the ingress.
- **I10 `token_hashes` is never pruned.** Every refresh adds two rows and used refresh rows are kept for reuse detection. Only legitimate grant holders cause the growth, so it is slow. Prune rows of revoked or expired credentials after their refresh TTL.
- **I11 Access logs carry request ids and upstream codes** (`/oauth/login?req=…`, `/oauth/callback?state=…&code=…`). Neither works alone: the request needs the binding cookie, and the upstream code needs our client secret and PKCE verifier. Keep the access logs private all the same.
- **Not recommended:** adding CSP `form-action 'self'`. Chrome enforces `form-action` on the redirect after a form POST, which would block the consent 303 to the client's redirect URI.

---

## Checked and sound

- **PKCE and codes [SDK + code]:**
  - `code_challenge` is required and S256-only (`authorize.py` model).
  - The SDK compares the verifier (`token.py:174-185`).
  - Codes are 256-bit, stored hashed, bound to their client (`grants.py:274-292`), expire after 5 minutes (SDK re-checks `expires_at`), and are redeemed with an atomic `DELETE … RETURNING` inside `BEGIN IMMEDIATE` (`grants.py:294-324`), so two parallel redemptions can't both succeed.
  - The token request's redirect_uri must equal the authorize one (`token.py:149-170`).
- **Redirect URI validation [SDK + probe]:**
  - Exact match against registered URIs (`shared/auth.py:102-111`).
  - Registration refuses `javascript:`, `data:` and `file:`, http on non-loopback hosts (`localhost.evil.com` and `127.0.0.1.nip.io` refused), and fragments.
  - The only redirect paths are the consent result (after sign-in and CSRF) and the SDK error path (M2).
- **Revocation completeness [code]:**
  - `token revoke` sets `revoked_at`. `grants.access()` checks `cred.status()` on every request (`grants.py:347-353`); `refresh_info` and `rotate` check it too (`:386-391`, `:407-412`). Access and refresh stop on the next request.
  - RFC 7009 with either token type revokes the whole grant (`provider.py:189-194`).
  - Bearer tokens can't be revoked through `/revoke`: their `client_id` is the credential id, which never matches (`revoke.py:79`), and `grant_of`/`revoke_grant` filter by type and kind.
  - Sequential refresh replay revokes the grant (tested).
- **`current()` [code]:** it resolves the credential from the `credential_id` claim that `load_access_token` set after a live status check. fastmcp's `get_access_token` reads the current request's scope (`dependencies.py:485-492`), so a revoked grant fails at the auth middleware first. `current()` doesn't re-check status, which is fine because the window is the same request.
- **Scope enforcement [code]:**
  - Every mutating tool goes through `VaultWriter.apply` (`writer.py:179`), `move` (`:335`) or `rename` (`:524`), each with `allows("write")`. That covers `submit_source`, `review_queue_add`, and `claim`/`complete`/`release` via rename.
  - The consent cap is recomputed on the server from the stored request, so a posted `scope=write` can't exceed `max_scope` (`pages.py:264-267`; tested).
- **CSRF, login CSRF, session fixation [code]:**
  - Each request is bound to an HttpOnly, SameSite=Lax cookie on `/oauth` (Secure on https) plus a CSRF token rotated on every render. Both are compared as SHA-256 hashes with `compare_digest`.
  - Consent GET and POST and the callback require the binding cookie.
  - Before sign-in, GET `/oauth/login` re-binds the request to whoever opens it. That only matters to someone who already holds the request id (the attacker's own request, i.e. M3).
  - Cross-site POSTs carry no Lax cookie. There is no session, so nothing to fix.
- **Upstream code injection or mix-up [code]:** each sign-in gets a fresh PKCE verifier and OIDC nonce, cleared once used. `state` is the 256-bit request id plus the cookie.
- **ID token verification [code + probe]:**
  - Signature checked against the provider's JWKS. `alg=none` is rejected, and HS256 with an RSA JWKS is rejected (`InvalidKeyTypeError`).
  - `iss`, `aud`, `exp` and `nonce` are essential: expired, missing-exp, string-exp and wrong-nonce tokens are all rejected.
  - The discovery `issuer` must equal the configured issuer; `email_verified` must be true for google/oidc. GitHub accepts only the primary verified email. Microsoft `common`/`organizations`/`consumers` are refused at start-up.
- **SSRF [code]:**
  - Upstream URLs come only from admin config, or from discovery of the configured https issuer; GitHub URLs are constants.
  - DCR `logo_uri`, `client_uri` and `jwks_uri` are stored but never fetched.
  - fastmcp CIMD (URL client ids) is not active, because `get_client` is overridden.
  - `submit_source` never fetches URLs.
  - The issuer must be https except on localhost (SDK `validate_issuer_url`, `routes.py:25-47`).
- **Timing [code + SDK]:** secrets are looked up by SHA-256 and compared with `compare_digest`; the SDK compares client secrets with `hmac.compare_digest` (`client_auth.py:109`).
- **Secrets at rest [code]:** request ids, browser nonces, CSRF tokens, codes, and access/refresh/bearer tokens are 256-bit and stored as SHA-256 (fast hashing is right for random secrets, D16). `tokens.db` is created 0600, with umask 077 for WAL/SHM.
- **Approval-code brute force (online) [code]:** 2^50 codes, 5 tries per request, 30 failures per 10 minutes server-wide: infeasible. (The lockout itself is the availability problem in M5.)
- **XSS, framing, caching [code]:**
  - Every dynamic value is passed through `html.escape`, with `quote=True` in attributes.
  - CSP is `default-src 'none'; frame-ancestors 'none'; base-uri 'none'`, plus `X-Frame-Options: DENY`, `no-store`, `nosniff` and `no-referrer`, on pages and on the kit's redirects.
  - Redirect `Location`s carry only validated request ids.
- **Log injection [code]:** `audit.log` is written with `json.dumps`, so attacker-supplied `client_name` and refusal reasons are escaped. No tokens, codes or verifiers are logged.

## Suggested fix order
1. M1: parameter caps and pending-request caps.
2. M2: an `/authorize` wrapper that never redirects on errors.
3. M3: reject userinfo, show the hostname, label the name as unverified.
4. M4: Google `hd`, Microsoft `xms_edov`/guest rejection, and a decision on GitHub org membership.
5. M5 and M6: per-IP throttling, faster pruning, evict instead of refusing.
6. L1–L3: revoke handler, atomic reuse revocation, auth-event audit and alerts.
7. L4–L6 and the Info items as time allows.

Tests to add along with the fixes:
- oversized `state` is rejected;
- `/authorize` with a bad scope renders a page and doesn't redirect;
- a redirect URI with userinfo is refused at `/register`;
- `/revoke` without `client_secret` revokes the grant;
- a concurrent refresh replay revokes the grant;
- Google sign-in without `hd` is refused.
