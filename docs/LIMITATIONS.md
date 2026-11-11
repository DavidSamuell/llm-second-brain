# Limitations

## Ignis behaviour verified during build (SPEC §8)

Tested on 2026-11-09 against:

| Component | Version |
|---|---|
| Ignis image | `nobbe/ignis@sha256:2049be7dfbbee5ee9fa5c0ae56efd8555ec57f2ac3275448b1c4318b8af49bb5` (monorepo `0.9.0`, label `com.thiefling.ignis.obsidian-version=1.14.4`) |
| Obsidian | 1.14.4 (Ignis' pinned version) |
| Browser | Headless Chromium 150.0.7871.24. Each "user" was a separate, isolated browser context (no shared cookies or localStorage). |
| Mounts | Vault at `/vaults/Docs`, Ignis state at `${DATA_PATH}/ignis` → `/app/data`, Obsidian cache in a named volume (plan D4) |

| # | Question | Result | Observed behaviour |
|---|---|---|---|
| 1 | Is workspace/layout state kept separate for each user? | **Fail: one layout is shared by all users** | The layout is saved in the vault at `.obsidian/workspace.json`. User A opened `Alpha`; a new user B loaded straight into `Alpha`. B then opened `Beta`, and A's live session stayed on `Alpha`. When A reloaded, A came back on `Beta`. Whoever wrote the layout last decides what the next person who loads or reloads sees. Mobile clients use a separate file, `.obsidian/workspace-mobile.json`. |
| 2 | Two users editing the same note | **Fail: the last save wins and the other user's text is lost** | **One at a time:** A typed, and B's open editor showed A's text within 5 s. **At the same time:** A and B both typed a different line. The file on disk ended up with B's line, and A's editor kept showing A's own line, out of sync with the file. A's next keystroke then saved A's version and erased B's line. Neither user is warned. |
| 3 | A note open in the editor changes on disk (§16.12) | **Pass when the editor is idle; fail for keystrokes not yet saved** | **Idle editor:** an atomic replace of the file (temp file + rename, the same way vault-mcp writes) appeared in the open editor within 510 ms. Typing afterwards saved the new content plus the typed text, so nothing was overwritten. **Mid-typing:** when the file was replaced while the user was typing, the editor reloaded the new content and dropped the keystrokes not yet saved. The typed text `\nA-typing-during-ext` was cut down to the characters typed after the reload (`ring-ext`). See [screenshot](assets/limitations/ignis-desktop.png). |
| 4 | Usability on mobile browsers | **Pass (emulated only)** | Tested in Chromium emulating an iPhone 12 (390×844, touch). Obsidian switched to its phone layout (`is-phone is-mobile`). The file drawer, opening a note, and typing all worked, and the edit was saved to disk. See [screenshot](assets/limitations/ignis-mobile-emulated.png). **Not checked on a real iOS Safari or Android Chrome device**, so on-screen keyboard and viewport behaviour there are unknown. |
| 5 | Read-only `.obsidian/plugins` mount (plan D28) | **Pass: Ignis starts and works** | The plugins directory was bind-mounted read-only, with `data.json` bind-mounted read-write as a single file. An allowlisted test plugin loaded from it, and its `saveData()` call succeeded through the read-write `data.json`. Trying to install a plugin from the UI (`app.plugins.installPlugin`, Calendar 1.5.10) failed with `ENOENT` and wrote nothing to disk. **Control run** with a writable plugins directory: the same install succeeded and put `main.js` into the vault. So the read-only mount is what blocks UI installs. |
| 6 | Obsidian version loaded with `OBSIDIAN_VERSION` set to the pinned value (plan D29) | **Pass** | The startup log said `Obsidian ready (v1.14.4).` on the first start and `Obsidian already set up (v1.14.4).` after that. The page title was `… - Obsidian 1.14.4`, and the Obsidian API reported version `1.14.4`. With `OBSIDIAN_VERSION=1.14.3` (older than the pin), Ignis **did not refuse to start**: it logged `OBSIDIAN_VERSION=1.14.3 is older than this build's Obsidian 1.14.4; using 1.14.4.` and ran 1.14.4. |
| 7 | Is there an HTML marker that lets check-exposure recognise Ignis (plan D35)? | **Pass: two stable markers found** | `GET /` without credentials returns `200` with `<meta name="apple-mobile-web-app-title" content="Ignis"/>` and an element `id="ignis-status"`. The `<title>` is just `Obsidian`, which is too generic to use. |

### Consequences for operators

- **Concurrent web edits lose data.** If two people type in the same note at the same moment, the last save wins and nobody is warned. Version history keeps only the versions that stayed on disk for the watcher's whole debounce window (5 s with no further change), so text overwritten sooner than that can't be restored. Agree who edits what, or let agents propose changes through the review queue.
- **An agent write can drop up to one autosave interval of a person's unsaved typing** in the same note. An agent's write only goes through if the note hasn't changed since the agent last read it. That check can't see keystrokes that haven't been saved yet, so it can't prevent this. Agents should prefer section tools (`replace_section`, `insert_after`) and `append`, which change less of the note.
- **Layout is shared.** Each user's open tabs and panes get replaced by whoever saved the layout last. This is cosmetic only; no note content is affected.
- **Community plugins are switched on or off per browser.** Obsidian keeps its "restricted mode" setting in each browser's localStorage (key `enable-plugin-<vault>`), not on the server. Allowlisted plugins run only in browsers where the user has turned community plugins on. The kit cannot change that setting for users. (Update after T1: Ignis also has a server-wide "Always trust plugins" switch; see [Restricted mode is per browser](#restricted-mode-is-per-browser).)
- **A mismatched `OBSIDIAN_VERSION` does not stop Ignis from starting.** A wrong value is silently replaced by Ignis' own pin. Check the version in the `Obsidian ready|already set up (vX)` log line instead.
- **Docker creates a missing bind-mount target as `root`.** If `.obsidian/plugins` doesn't exist in the vault when the read-only mount is set up, Docker creates it owned by root. Ignis then can't use that directory, even after the mount is removed. The plugin setup that runs before Ignis starts must create it first, owned by `PUID:PGID`.

### Not verified

- Real mobile devices (iOS Safari, Android Chrome).
- Whether Obsidian's per-device "override config folder" setting would let a user load plugins from a writable folder somewhere other than `.obsidian/plugins`.

## People and agents editing the same note

Ignis takes no lock. vault-mcp and the other kit services share an advisory lock (`flock` on `DATA_PATH/locks/vault.lock`), but Ignis knows nothing about it and saves whenever the browser autosaves.

- **An agent write can drop up to one autosave interval of someone's unsaved typing.** An agent's write only goes through if the file still has the version the agent read (`base_version`). Text the browser hasn't saved yet isn't in the file, so the check can't see it. When the agent's write lands, the open editor reloads the file and loses those keystrokes (T1, row 3). Section tools (`replace_section`, `insert_after`) and `append` change less of the page, but they don't remove the risk.
- **A browser save can be overwritten in a very small window.** vault-mcp reads the file a final time right before it renames its new version into place. A browser save that lands between that read and the rename is overwritten, and it is not in history either: the watcher only sees the file after the agent's version is in place. In the T3 measurements this window was 0.04–0.1 ms, with no lost saves in 3 runs of 18 interleaved saves. It can't be zero without a lock in Ignis.
- **No "someone is typing" guard.** Refusing agent writes to recently changed files was considered and not built (decision recorded in `tasks/impl-notes.md`). If people and agents work on the same pages, have agents propose changes with `review_queue_add`, or mark the pages `maintainer: human`, which agents can't edit.

## Web edits are not attributed to a person

Ignis has no user accounts, so nothing records who changed a file in the browser.

- vault-watcher labels each version in history with a source:
  - `git`: the change arrived while git-sync held the vault lock;
  - `web`: any other change it saw as it happened;
  - `unknown`: a change found by the startup or periodic scan;
  - `system`: the watcher's own review-queue items.
- vault-mcp writes are linked to their audit-log entry, which names the credential and its owner.
- `web` means "not vault-mcp and not git", not "the browser". Edits made in a shell on the server, or by an Ignis server plugin such as Headless Sync, are labelled `web` too.
- Your ingress may log who signed in to the editor and when (Cloudflare Access logs, oauth2-proxy logs), but not which files they changed.
- There are no per-user permissions in the editor either. Everyone who can log in can read and change every file, including the folders hidden from agents by `read.deny`. Run a separate deployment for content a group must not see.

## Restricted mode is per browser

Obsidian only runs community plugins after a person switches off "restricted mode", and it remembers that choice per browser (T1). For the kit this means:

- Allowlisted plugins are installed on the server for everyone, but run only in browsers where the person has turned community plugins on. A new device, another browser, or cleared site data starts in restricted mode again.
- The kit's default allowlist is empty, so out of the box nothing depends on community plugins.
- Ignis 0.9.0 has a server setting, "Always trust plugins for this vault" (in Obsidian's settings under Ignis). Its description says it turns community plugins on for the vault in every browser that opens it. We found it while writing [SECURITY.md](SECURITY.md) but have not tested it in a browser. Any person who can log in can switch it on or off, because Ignis has no roles.

## Symlinks are refused

vault-mcp, version history, the search index, the secret scan, and backup/restore all ignore any path that goes through a symlink, even one that points to another place inside the vault. MCP calls on such a path fail with `forbidden`. This stops a link from giving one folder another folder's permissions (see [SECURITY.md](SECURITY.md#symlinks)). Ignis still follows symlinks, so a symlinked file can be edited in the browser but has no history and is not backed up. If your existing vault uses symlinks, replace them with real files or folders before adopting the kit.

## Search is full-text only

`search` uses a SQLite FTS5 index (`DATA_PATH/index.db`) over title, headings, path, frontmatter and body.

- Every word in the query must appear in the page. Words are matched literally: no stemming (`deploy` doesn't find `deployment`), no synonyms, no meaning-based search. Accents are ignored.
- Semantic (embedding) search is out of scope for v1 (SPEC §7.2). It may be added once vaults grow beyond about 2,000 pages.
- `read.deny` folders are never indexed, and a credential limited by `--folders` only gets results from its folders.
- The index is rebuilt at startup, and on demand with `scripts/reindex`.

## Ingress plan limits

- **Cloudflare (`INGRESS=cloudflare`).**
  - The free Zero Trust plan allows **50 users** in Access; beyond that you need a paid plan.
  - Cloudflare's request body limit is **100 MB per request** on the Free and Pro plans, so larger attachments can't be uploaded through the editor. Add them through git or the server's file system instead.
  - Cloudflare terminates TLS, so it sees all traffic in clear inside its network.
  - Details in [ingress/cloudflare.md](ingress/cloudflare.md).
- **Tailscale.** The free Personal plan is for personal, non-commercial use. A team using the [Tailscale recipe](ingress/tailscale.md) for work needs a paid Tailscale plan, or [Headscale](ingress/headscale.md).
- **Caddy (`INGRESS=caddy`).** It has no user cap, but needs a public IP with ports 80 and 443 open.

## Ignis: maturity and legal basis

- **What the kit ships.** The kit (MIT licence) references Ignis only as a container image, pinned by digest (`IGNIS_IMAGE`), and never vendors or modifies its code (SPEC §2, §17). Ignis is licensed **AGPL-3.0-or-later** ([source](https://github.com/Nystik-gh/ignis)). If you modify Ignis and let people use the modified version over a network, the AGPL requires you to offer them your modified source. Running the unmodified image is how the kit uses it.
- **No Obsidian code in the kit or in Ignis.** On first start the Ignis container downloads the pinned Obsidian version itself (`Downloading Obsidian v1.14.4...` in its log) and caches it in the `obsidian-app` volume. Ignis' legal notice (`LEGAL.md` in its repository) says it contains no Obsidian code, loads Obsidian's unmodified scripts, is not affiliated with Obsidian, and relies on the interoperability provisions of the EU Software Directive.
- **Maturity.** Ignis is a young project (version 0.9.0 at the pinned digest). Upgrades can change behaviour the kit relies on. Follow UPGRADING.md and its smoke test before moving the pin.
- **Escape hatch.** The vault is plain Markdown. If Ignis becomes unusable or unacceptable, any editor that works on files keeps working: desktop Obsidian on a synced or git-cloned copy, a code editor over SSH, or a git web UI with git-sync on. vault-mcp, history, search and git-sync don't depend on Ignis.

SPEC §17 recommends a legal review before using Ignis in production at a company. The kit can't make that decision for you.

## Obsidian's terms of use

Everyone who uses the editor runs Obsidian in their browser, so Obsidian's [Terms of Service](https://obsidian.md/terms) and [licence overview](https://obsidian.md/license) apply to your deployment. As read on 2026-11-10 (the terms say "Last updated February 20, 2025"):

- Obsidian may be used **for free for commercial purposes** by for-profit organisations and government entities. A Commercial licence is optional.
- The terms also restrict, among other things:
  - making the Software "available for access by third parties";
  - creating derivative works or modifying it;
  - using it "to provide a service for others";
  - installing it anywhere other than "on machines operated by or for you".

What the kit can say:

- It does not include, modify or redistribute Obsidian.
- Each deployment downloads Obsidian itself, onto the deployer's own server.

What it can't say:

- That serving Obsidian through Ignis to your team fits these terms.
- That hosting it for another organisation or for customers does. That use is closer to "provide a service for others".

Have the terms reviewed for your use before production, and check them again when Obsidian updates them.
