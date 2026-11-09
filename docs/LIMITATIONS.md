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
- **Community plugins are switched on or off per browser.** Obsidian keeps its "restricted mode" setting in each browser's localStorage (key `enable-plugin-<vault>`), not on the server. Allowlisted plugins run only in browsers where the user has turned community plugins on. The kit cannot change that setting for users.
- **A mismatched `OBSIDIAN_VERSION` does not stop Ignis from starting.** A wrong value is silently replaced by Ignis' own pin. Check the version in the `Obsidian ready|already set up (vX)` log line instead.
- **Docker creates a missing bind-mount target as `root`.** If `.obsidian/plugins` doesn't exist in the vault when the read-only mount is set up, Docker creates it owned by root. Ignis then can't use that directory, even after the mount is removed. The plugin setup that runs before Ignis starts must create it first, owned by `PUID:PGID`.

### Not verified

- Real mobile devices (iOS Safari, Android Chrome).
- Whether Obsidian's per-device "override config folder" setting would let a user load plugins from a writable folder somewhere other than `.obsidian/plugins`.
