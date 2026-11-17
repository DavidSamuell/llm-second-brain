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

The human-facing folders hold the current documentation; agents maintain them and people may edit them too. The wiki is an atomic reference map, linking to those docs rather than copying their narrative.

| Folder | Written by | What goes there |
|---|---|---|
| `Home.md` | people | Start page |
| `domains/` | agents and people | The team's docs, grouped by domain (a product area such as `asr`) and then by project. See below. |
| `shared/` | agents and people | Specs, decisions, pipelines and runbooks that span several domains |
| `attachments/` | people | Images and files you link from pages (Obsidian puts pasted images here) |
| `wiki/` | agents | Reference pages built from sources: `components/`, `concepts/`, `glossary/`, `people-teams/`, `sources/` |
| `raw/` | anyone | Incoming sources; explicit ingest/document requests process them in the same session |
| `_system/` | agents | The wiki index, activity log, review queue, lint report, and page templates |

In the editor, `_system/`, `raw/_processing/` and `raw/_processed/` are left out of search and the graph view.
Use the "Wiki index" and "Review queue" links on `Home` to reach `_system/index.md` and
`_system/review-queue.md`.

### Domains and projects

Each domain has a folder with a `README.md` overview. Each project lives inside its domain, with
its own `README.md` and up to four subfolders:

```
domains/asr/README.md                         the ASR domain: what it covers, its projects
domains/asr/parakeet/README.md                the Parakeet project: goal, status, owners, repos, links
domains/asr/parakeet/specs/                   product specs and architecture
domains/asr/parakeet/decisions/               decisions (ADRs), numbered: 0001-use-gpu2.md
domains/asr/parakeet/pipelines/               data, training and build pipelines
domains/asr/parakeet/runbooks/                how to run things, onboarding, what to do when things break
domains/asr/zipformer/README.md               another project in the same domain
domains/asr/shared/pipelines/                 docs for the whole domain (also specs/, decisions/, runbooks/)
shared/runbooks/                              docs for several domains (also specs/, decisions/, pipelines/)
```

Because every overview is called `README.md`, link to one with its full path:
`[[domains/asr/parakeet/README]]`. `shared` is a reserved name and is never a project.

Where a doc lives follows the projects listed in its `project:` frontmatter: one project, that
project's folder; several projects of one domain, `domains/<domain>/shared/`; projects in
different domains, top-level `shared/`. When an agent learns that another project uses a whole
doc, it adds that project and moves the doc to match; the move updates every link to it. A doc
that only partly applies to another project stays put and is linked from that project's
`README.md`. The nightly lint moves docs that sit in the wrong folder, or files a review item
when it can't.

People create domains. An agent creates a new domain only after you agree to it in the session.
When an agent documents a project that fits no existing domain, it asks you to pick a domain or
approve a new one. When no one is there to ask (the nightly job), it still writes the wiki pages,
source summary and graph links, but holds back the project's docs and adds one review item,
"Project <x> needs a domain", with a suggestion. Pick or create the domain, then ask an agent to
write the project's docs.

## Editing pages

- Click a page in the folder list and type. Obsidian saves automatically; there is no Save button.
- Link to other pages with `[[` and start typing a page name. Renaming a page in Obsidian updates
  links to it.
- File and folder names are lowercase with dashes: `domains/search/search-revamp/README.md`.
- Please don't type in a note at the same moment as someone else. See [Known limits](#known-limits).

### Starting a page from a template

1. Create a new note in the right folder (for example `domains/asr/parakeet/runbooks/`).
2. Open the command palette (`Ctrl+P`, or `Cmd+P` on a Mac) and run **Templates: Insert template**.
3. Pick a template.

Templates for people's pages: `domain`, `project-overview`, `architecture`, `adr`, `guide`, `product-spec`,
`pipeline`, `runbook`. The others (`wiki-component`, `wiki-concept`, `glossary-term`, `wiki-team`,
`source-summary`) are for wiki pages, which agents usually write.

Each template starts with a block like this, called frontmatter:

```yaml
---
title: "Restarting the ingest job"
type: runbook
maintainer: shared
project: ["[[domains/asr/parakeet/README]]"]
sources: []
graph: {}
tags: []
updated: "2026-11-17"
---
```

Keep the dashes and the `name: value` lines intact; agents and search read them.

## What `maintainer` means for you

Every page says who looks after it. With the v0.2 example policy:

| Value | What it means |
|---|---|
| `maintainer: human` | A page a person wrote by hand. Agents may still update it when its information is out of date, and every such change puts a `#review/agent-edit` item in the review queue so you can check it (your admin can switch this off). |
| `maintainer: agent` | A page agents keep up to date, usually in `wiki/`. You may still edit it. Agents re-read before changing it; this is not a lock against simultaneous editing. |
| `maintainer: shared` | Both people and agents edit it. Agents make small, section-sized changes. The review queue is a shared page. |

With that policy, pages with no `maintainer` line can be edited by agents in `wiki/`, `raw/`,
`_system/`, `domains/` and `shared/`. Existing deployments keep their
old permissions until an admin opts in. Agents cannot edit the protected rules: `AGENTS.md`,
`CLAUDE.md`, `skills/`, the templates or the graph ontology. After the watcher records an
editor save, agents wait 90 seconds by default before updating that page. Unsaved typing and
saves not yet recorded are not protected; avoid simultaneous editing of the same page.

## The review queue

`_system/review-queue.md` is a to-do list of things an agent wants a person to decide. Agents add
an item when they:

- changed a `maintainer: human` page (tagged `#review/agent-edit`: check the change and tick it), or want to change one when the admin has kept `read_only`,
- find two sources that disagree,
- couldn't fetch a source someone submitted,
- want to rename a page they aren't allowed to edit,
- found a new project that fits no domain, while nobody was there to ask.

An item looks like this:

```
- [ ] 2026-11-10 09:30Z [[domains/asr/parakeet/README]] — Out of date — Mention the v2 API — by alice-laptop (alice@example.com) #review ^rq-01abc
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

## Asking your agent to document or ingest

With the generated authoring plugin installed in Claude Code or omp, ask in plain language:

> Create pipeline docs for this project and ingest the relevant repo knowledge into the vault.

The agent reads the relevant committed README/code, records a cited source summary, creates or
updates the human doc, links it from the project, adds the wiki reference and typed relationships,
and completes the source **during this session**. It does not wait for the nightly job.

| Command | When to use it |
|---|---|
| `/docs-vault:document pipeline docs for this project` | Create or update a project, spec, pipeline or runbook from the repo and conversation |
| `/docs-vault:ingest <file, URL or pasted text>` | Read/fetch the source, submit it and fully ingest it now |
| `/docs-vault:ask <question>` | Read-only answers from the vault, with citations and explicit gaps |
| `/docs-vault:lint [folder]` | Check and repair permitted docs, citations, structure and graph relationships now |
| `/docs-vault:connect` | Check sign-in and connectivity |

Codex gets the same skills; use `$docs-ingest`, `$docs-query`, `$docs-lint`, `$docs-capture` or
plain requests rather than these namespaced commands. Automatic session hooks are configured
separately; the manual commands do not require them.

Durable findings are captured as they become settled during work, not only at the end: decisions,
architecture, interfaces, pipeline/runbook steps, incidents and conventions. The agent excludes
secrets, personal data, guesses and transient debugging. Tell it not to capture to opt out.

Human docs keep the current state first, cite a source summary or `repo@commit:path`, and record
one dated `## Change log` line per update. An unresolved contradiction becomes a review item;
supported obsolete statements are replaced in place, with the older claim preserved in provenance.

You may also paste a Markdown source directly into `raw/`. That alone only queues it: ask a
connected agent to ingest it, or let an enabled scheduled agent pick it up. A successful manual
ingest leaves its source in `raw/_processed/`, with summary, docs, index and log updated. If it
fails, the agent reports any pages already changed and releases the source for another attempt.
More detail and bulk imports: [IMPORTING.md](IMPORTING.md).

## Page history and restoring a version

The server keeps a copy of every version of every page: a browser edit is saved as a new version
once the page has been left alone for about 5 seconds, and every agent edit is saved too. By
default versions are kept for 90 days, and at least the last 10 versions of each page are always
kept, however old.

There is no history view in the editor; restoring is done by an admin. Tell them the page path
and roughly when it was last right. **Admin:**

```bash
scripts/history list domains/asr/parakeet/README.md
# TIMESTAMP (UTC)             EVENT   SOURCE   VERSION      AUDIT ID                   NOTE
# ...one line per version; SOURCE is web (browser), mcp (agent), git, system or unknown

scripts/history restore domains/asr/parakeet/README.md --at 2026-11-09T12:00:00Z   # as it was at that time
scripts/history restore domains/asr/parakeet/README.md                             # latest version in history
scripts/history restore-deleted domains/asr/parakeet                              # bring back deleted pages in a folder
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
