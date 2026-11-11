# User guide

This guide is for people who read and edit the team docs in a web browser. You don't need to
know anything about servers or AI tools to use it. Admin commands are marked as such: pass them
on to whoever runs the server.

## Signing in

1. Open the editor address your admin gave you (it looks like `https://docs.example.com`).
2. Sign in on the login page that appears. This is your company account (for example Google,
   Microsoft, or GitHub), or whatever your admin set up in front of the editor.
3. The vault opens in a browser version of Obsidian. The folder list is on the left; `Home` is
   the start page.

The editor itself has no user accounts. Everyone who gets past the login page can read and edit
every page, and the history doesn't record who made a browser edit.

If your admin set the server up for SSH-only access, you open `http://127.0.0.1:8081` through an
SSH tunnel instead; they will give you the exact command.

## How the vault is organised

Some folders belong to people, some to the AI agents that keep the wiki up to date.

| Folder | Written by | What goes there |
|---|---|---|
| `Home.md` | people | Start page |
| `projects/` | people | Project overviews, plans, decisions (ADRs) |
| `specs/` | people | Product specs and architecture docs |
| `pipelines/` | people | Data and build pipelines |
| `runbooks/` | people | How to run things, and what to do when they break |
| `attachments/` | people | Images and files you link from pages (Obsidian puts pasted images here) |
| `wiki/` | agents | Reference pages built from sources: `components/`, `concepts/`, `glossary/`, `people-teams/`, `sources/` |
| `raw/` | anyone | Sources waiting to be turned into wiki pages |
| `_system/` | agents | The wiki index, activity log, review queue, lint report, and page templates |

`_system/`, `raw/_processing/` and `raw/_processed/` are left out of search and the graph view.
Use the "Wiki index" and "Review queue" links on `Home` to reach `_system/index.md` and
`_system/review-queue.md`.

## Editing pages

- Click a page in the folder list and type. Obsidian saves automatically; there is no Save button.
- Link to other pages with `[[` and start typing a page name. Renaming a page in Obsidian updates
  links to it.
- File names are lowercase with dashes: `projects/search-revamp.md`.
- Please don't type in a note at the same moment as someone else. See [Known limits](#known-limits).

### Starting a page from a template

1. Create a new note in the right folder (for example `runbooks/`).
2. Open the command palette (`Ctrl+P`, or `Cmd+P` on a Mac) and run **Templates: Insert template**.
3. Pick a template.

Templates for people's pages: `project-overview`, `architecture`, `adr`, `guide`, `product-spec`,
`pipeline`, `runbook`. The others (`wiki-component`, `wiki-concept`, `glossary-term`, `wiki-team`,
`source-summary`) are for wiki pages, which agents usually write.

Each template starts with a block like this, called frontmatter:

```yaml
---
title: "Restarting the ingest job"
type: runbook
maintainer: human
tags: []
updated: "2026-11-10"
---
```

Keep the dashes and the `name: value` lines intact; agents and search read them.

## What `maintainer` means for you

Every page says who looks after it:

| Value | What it means |
|---|---|
| `maintainer: human` | A page people write. Agents never change it. If an agent thinks it is wrong or out of date, it adds an item to the review queue instead. All the people templates use this. |
| `maintainer: agent` | A page agents keep up to date, usually in `wiki/`. You may still edit it, and your edit wins: agents re-read the page before they change it. |
| `maintainer: shared` | Both people and agents edit it. Agents make small, section-sized changes. The review queue is a shared page. |

Pages with no `maintainer` line are treated as people's pages everywhere except `wiki/`, `raw/`
and `_system/`. Don't change the `maintainer` value of an existing page to let an agent in; ask
whoever owns the page.

## The review queue

`_system/review-queue.md` is a to-do list of things an agent wants a person to decide. Agents add
an item when they:

- want to change a `maintainer: human` page,
- find two sources that disagree,
- couldn't fetch a source someone submitted,
- want to rename a page they aren't allowed to edit.

An item looks like this:

```
- [ ] 2026-11-10 09:30Z [[projects/alpha]] — Out of date — Mention the v2 API — by alice-laptop (alice@example.com) #review ^rq-01abc
```

That is: when, which page, the problem, the suggested change, and which agent (and whose
credential) added it.

To handle an item:

1. Open the page it links to and make the change yourself, or decide not to.
2. Tick the checkbox. Don't edit or delete the rest of the line.

The agents' tidy-up job (lint) later moves ticked items to `_system/review-archive.md`.

### `#review/high` items: possible secrets

Items tagged `#review/high` come from the automatic secret scan. Every time a page changes, the
server checks it for things that look like passwords, API keys, private keys or connection
strings. When it finds one it adds an item like `Possible secret (…)` with the page and a masked
hint, and alerts the admin. The secret itself is never repeated in the item.

What to do:

1. Remove the secret from the page.
2. Tell the owner of that credential so they can rotate it (issue a new one and switch off the
   old one). The item says the same: older versions of the page in the server's history still
   contain the secret, and there is no way to purge them, so treat it as leaked. Rotating the
   credential is the fix; deleting the text only stops it spreading further.
3. Tick the item.

## Getting sources into the wiki

The `wiki/` is built from sources: design docs, meeting notes, chat threads, web pages, and so on.
You don't write wiki pages yourself; you hand the source over and an agent summarises it, with
citations back to the source.

Two ways to hand over a source:

- **Ask your AI assistant** (Claude, ChatGPT, Cursor and so on, once it is connected to the vault;
  see [CONNECT-AGENTS.md](CONNECT-AGENTS.md)). For example: *"Fetch this Google Doc and submit it
  to the docs vault as a source."* The assistant reads the content with its own tools and calls
  `submit_source`. If it can only pass a link, the vault stores the link and the ingest agent
  fetches it later. The vault server itself never opens links.
- **Drop a Markdown file into `raw/`.** Create a new note directly in `raw/` (not in a subfolder),
  paste the text in, and give it a clear name such as `raw/2026-11-10-search-design-notes.md`.

An agent then picks the source up, writes a summary in `wiki/sources/`, updates the related wiki
pages, and moves the source to `raw/_processed/`. If your admin runs the scheduled agent, this
happens about once an hour; otherwise ask someone with a connected AI assistant to run an ingest.
More detail, including bulk imports, is in [IMPORTING.md](IMPORTING.md).

## Page history and restoring a version

The server keeps a copy of every version of every page: a browser edit is saved as a new version
once the page has been left alone for about 5 seconds, and every agent edit is saved too. By
default versions are kept for 90 days, and at least the last 10 versions of each page are always
kept, however old.

There is no history view in the editor; restoring is done by an admin. Tell them the page path
and roughly when it was last right. **Admin:**

```bash
scripts/history list projects/alpha.md
# TIMESTAMP (UTC)             EVENT   SOURCE   VERSION      AUDIT ID                   NOTE
# ...one line per version; SOURCE is web (browser), mcp (agent), git, system or unknown

scripts/history restore projects/alpha.md --at 2026-11-09T12:00:00Z   # as it was at that time
scripts/history restore projects/alpha.md                             # latest version in history
scripts/history restore-deleted projects                              # bring back deleted pages in a folder
```

`--at` takes a UTC time (ISO 8601) or a timestamp copied from `history list`. A restore doesn't
erase anything: it saves the old content as a new version, so it can itself be undone.

## What not to put in the vault

- **Secrets:** passwords, API keys, tokens, private keys, database connection strings. Link to
  where the secret is stored (your password manager or secret store) instead. Agents are blocked
  from writing anything that looks like a secret; browser edits are scanned after saving, as
  described above.
- Anything you wouldn't want every vault user, and the AI tools they connect, to read. Admins can
  hide whole folders from AI tools (by default `private/` and `hr/`), but every signed-in person
  still sees everything in the editor.

## Known limits

These come from tests of the editor the kit uses; the full list is in
[LIMITATIONS.md](LIMITATIONS.md). [SECURITY.md](SECURITY.md) explains who can see and change what.

- **An agent edit can drop a few seconds of your typing.** If an agent changes a note while you
  are typing in it, the editor reloads the note and loses whatever you typed since the last
  autosave. Agents usually edit `wiki/` pages and the review queue, not people's pages. If you are
  writing in an agent or shared page and the text jumps, check that your last sentence is still
  there.
- **Two people typing in the same note at once lose text.** The last save wins and nobody is
  warned. Agree who edits a page, or take turns.
- **The open tabs and layout are shared.** When you reload, you may land on whatever page someone
  else had open. No content is affected.
- **Community plugins must be switched on in each browser.** Obsidian stores that switch in your
  browser, not on the server. By default the kit installs no community plugins, so you only need
  this if your admin has added some: open Settings → Community plugins and turn them on, once per
  browser.
- **Browser edits are not attributed.** History shows a browser edit as `web`, without your name.
- **Phones:** the editor worked in an emulated iPhone browser during testing; real phones haven't
  been tested.
