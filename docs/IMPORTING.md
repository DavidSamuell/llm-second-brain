# Importing documents

This guide is for team members and admins who want to bring existing knowledge into the vault:
documents from Confluence, Notion, Google Docs, Slack, repositories, web pages, or an existing
folder of Markdown files.

There are two kinds of import:

- **As sources.** The document goes through `raw/` while an agent writes a source summary,
  current human-facing docs, and atomic wiki references with typed links. The original stays in
  `raw/_processed/` for provenance. Explicit ingest/document requests finish in the current session.
- **As existing human docs.** Files go straight into a project folder such as
  `domains/asr/parakeet/specs/`, or into `domains/<domain>/shared/` or `shared/` when they serve
  a whole domain or several domains. With the v0.2 example policy agents may maintain them too.
  A manual edit or `maintainer: human` label is not a permanent lock: permitted human-page edits
  need a summary and create a review notice. Admins can retain `human_pages: read_only`.

## How sources get in

### An agent fetches, then submits

The vault server never fetches anything from the internet (plan decision D14): fetching
arbitrary URLs from the server would let anyone with a token make it reach internal addresses.
Fetching is the job of the agent you are talking to, using its own tools and connectors (web
fetch, GitHub, Google Drive, Slack, Notion and so on).

Ask your connected assistant (see [CONNECT-AGENTS.md](CONNECT-AGENTS.md)), for example:

> Fetch the "Search ranking design" doc from Google Drive, ingest it into the docs vault now,
> update the relevant project/spec docs and wiki pages, and keep its origin as "Google Drive".

The agent calls:

```
submit_source(title="Search ranking design", origin="Google Drive", content="<the Markdown>", url="<link, optional>")
```

The server writes `raw/<date>-<slug>.md`, for example `raw/2026-11-10-search-ranking-design.md`
(with `-2`, `-3`… if the name is taken that day), with frontmatter recording `title`, `origin`,
`url`, who submitted it (`submitted_by`, `submitted_owner`), `submitted_at` and a
`content_sha256`.

Limits: `content` must be text (convert PDFs and office files to Markdown first); the file may
be at most `max_file_mb` (default 10 MB); lines that look like secrets are refused with
`secret_detected`.

In Claude Code or omp, `/docs-vault:ingest <file, URL or text>` performs the whole lifecycle,
not just `submit_source`. The low-level tool by itself only queues a source; use it alone when
you deliberately want a pending item for a later ingest.

### Repository documentation and durable findings

Run `/docs-vault:document pipeline docs for this project`, or ask the same in plain language.
The agent identifies the remote and commit, reads the relevant README/code, and submits a short
summary with origin `repo:<host/org/repo>@<commit>`. Each fact cites `repo@commit:path`; `.env`,
credentials and large code dumps are excluded. Only committed code is cited as that revision.
The source is claimed, summarized, used to update the project and its human docs plus wiki/graph,
indexed, logged and completed before the command returns.

`project_context(repo=...)` recognizes equivalent SSH/HTTPS remotes and loads readable project
summary/status, one-hop relationships, open review items and recent changes within 4,000
serialized characters. It finds projects only at `domains/<domain>/<project>/README.md`.
Unknown or ambiguous projects return `project: null`; a document request may create the missing
project from cited repo facts. The agent asks you which domain it belongs in; people create
domains, and an agent adds a new one only when you agree. An unattended run (the nightly job)
doesn't guess: it writes the source summary and wiki/graph, holds back the project's docs, and
adds one review item, "Project <x> needs a domain".

`docs-capture` uses the same lifecycle whenever a finding becomes durable during a session.
It is not an end-of-session dump. Session-hook installation is separate from the manual commands;
neither requires sharing one person's vault credentials with the team.

### URL-only submissions

If the agent can't read the content itself, it can pass just the link:

```
submit_source(title="Postgres 17 release notes", origin="web", url="https://www.postgresql.org/docs/17/release-17.html")
```

The server checks that it is an `http`/`https` URL and stores a stub: frontmatter with the `url`,
and a body saying the content still has to be fetched and must be treated as data. Whoever
ingests the stub fetches the URL with their own tools. If they can't, they release the source
and add a review-queue item explaining why.

The scheduled `agent-runner` can fetch stubs itself: it runs Claude Code with
`--allowedTools "mcp__vault__*,WebFetch"`. A team member's agent with web access can ingest them
too.

### Dropping files into `raw/`

Anyone can put a Markdown file **directly** in `raw/`:

- in the browser editor: create a new note in `raw/` and paste the text;
- on the server (admins): copy files into `<VAULT_PATH>/raw/`.

Only files directly in `raw/` can be claimed for ingest; files in subfolders of `raw/` are
ignored. Optional but useful: start the file with the same frontmatter `submit_source` writes, so
the summary can cite where it came from:

```yaml
---
title: Search ranking design
origin: Google Drive
url: https://docs.google.com/document/d/...
---
```

## Importing an existing folder of Markdown

All commands run on the server, in the kit folder. Replace `/srv/vault` with your `VAULT_PATH`
and `1000:1000` with your `PUID:PGID` from `.env`. Files copied as another user (for example
root) can't be edited in the browser.

### The whole folder is an existing Obsidian vault

Point `VAULT_PATH` in `.env` at it and run setup with `--adopt`:

```bash
scripts/setup --adopt
```

Setup copies in the kit's files that don't exist yet (`AGENTS.md`, `Home.md`, the folder tree,
`_system/`, templates, skills) and never overwrites an existing file. Without `--adopt`,
interactive setup asks first; non-interactive setup leaves a vault that has no `AGENTS.md`
unchanged and warns that agents will lack the rules. After adopting, move your notes into the
kit's folders where they fit (`domains/<domain>/<project>/…`, `domains/<domain>/shared/…` or
`shared/…`). Which pages an agent may maintain is controlled by the existing
`vault-mcp.yaml`; adoption does not widen that policy automatically.

### As people's pages

```bash
mkdir -p /srv/vault/domains/asr/parakeet/specs/legacy
cp -r ~/old-docs/. /srv/vault/domains/asr/parakeet/specs/legacy/
chown -R 1000:1000 /srv/vault/domains/asr/parakeet/specs/legacy
```

Pick the domain and project the docs belong to; create `domains/asr/README.md` and
`domains/asr/parakeet/README.md` from the `domain` and `project-overview` templates if they
don't exist yet. Docs that span several domains go under `shared/` instead.

Under the v0.2 example policy, agents may maintain these folders even without a `maintainer`
field. Prefer `maintainer: shared` for collaboratively maintained docs. `maintainer: human`
requires a change summary and review notice in `edit_with_notice` mode; it is read-only in
`read_only` mode. History records incoming files, search/graph index readable pages, and the
secret scanner checks changed text. Existing policies keep their permissions until explicitly updated.

### As sources for the wiki

Copy every Markdown file into `raw/` as a flat list, turning the old folder path into the file
name so names don't collide:

```bash
cd ~/old-docs
find . -type f -name '*.md' -print0 | while IFS= read -r -d '' f; do
  name=$(printf '%s' "${f#./}" | tr '/ ' '--' | tr '[:upper:]' '[:lower:]')
  cp -n "$f" "/srv/vault/raw/$name"
done
chown 1000:1000 /srv/vault/raw/*.md
```

Each file becomes one source and one ingest. Very short notes are better merged into one file per
topic first; very long ones split by topic.

## Bulk imports

Things to plan for when importing hundreds of documents:

- **Rate limits.** Each `submit_source` is one write. The default limits per credential are 30
  writes a minute and 2000 a day (`rate_limits` in `vault-mcp.yaml`). An agent that hits them gets
  `rate_limited` with `retry_after_s`. Ingesting a source takes several writes (summary, page
  edits, index, log, claim and complete). Copying files into `raw/` on the server avoids the
  submit writes.
- **Ingest throughput.** `agent-runner`, if enabled, runs an ingest every hour (`INGEST_CRON`) and
  handles at most `INGEST_MAX_SOURCES` sources per run (default 10). Each run is also capped at
  `AGENT_MAX_TURNS` model turns (default 200). Both are set in `.env`; restart agent-runner after
  changing them. To run an extra round now (admin):

  ```bash
  docker compose exec agent-runner run-job ingest
  ```

  Several agents can ingest at the same time (team members' agents with the `docs-ingest` skill
  plus `agent-runner`); leases stop two of them from taking the same source.
- **Cost.** Every ingest is a model run. `AGENT_MONTHLY_BUDGET` caps agent-runner's spend; when it
  is reached, jobs are skipped and an alert is sent.
- **Secret scan.** Old documents often contain passwords or keys. Files copied onto the server
  are scanned after they land: expect `#review/high` items in `_system/review-queue.md` and alerts
  to `ALERT_WEBHOOK`. Remove each secret and have it rotated (see the
  [user guide](USER-GUIDE.md#the-review-queue)). A `submit_source` whose content contains
  something that looks like a secret is refused with `secret_detected`, so agents have to replace
  it with `[secret removed]` before submitting. The same check stops ingest agents from copying a
  secret from a source into a wiki page.
- **Git backup.** If `GIT_REMOTE` is set, git-sync commits and pushes the imported files, including
  `raw/_processed/`. Set `GIT_LFS=true` before importing binaries.
- **Check the result.** `_system/log.md` gets one line per ingest; `raw/` should empty out over
  time; leftovers usually mean a failed ingest with a review item explaining why.

## The ingest procedure

This is what an ingesting agent does for each source. It is defined in the vault's `AGENTS.md`
(section 9) and the `docs-ingest` skill.

1. `list("raw")` and pick a file directly in `raw/`.
2. `claim_source(path)`. The file moves to `raw/_processing/` with a lease (default 30 minutes,
   up to 1440 with `ttl_minutes`). If another agent has it, the call fails with
   `already_claimed` and the agent picks another file.
3. `read` the claimed file. A URL stub is fetched with the agent's own tools; if that fails, the
   agent calls `release_source` and adds a review item.
4. Write the summary `wiki/sources/<same name as the raw file>` from the `source-summary`
   template: what it is, key facts, decisions, people and systems, open questions, and the origin.
5. Update or create cited atomic wiki references and the affected human docs under
   `domains/<domain>/<project>/` (or the domain's `shared/` folder, or top-level `shared/`).
   Link each doc from its project's `README.md`; wiki pages use
   `documented_in` rather than duplicate narrative. Edit only affected sections and add one dated
   change-log line. Supported newer claims replace obsolete text; unresolved contradictions
   become review items. Human-page edits follow the configured policy and summary requirement.
   A new project that fits no existing domain is not filed anywhere without a person: the agent
   asks, or, when unattended, skips the project's docs and adds a "Project <x> needs a domain"
   review item.
6. Add new pages to `_system/index.md`.
7. Append one line to `_system/log.md`.
8. `complete_source(path, summary_page="wiki/sources/<name>.md")`. The source moves to
   `raw/_processed/` and the lease is released. The summary page must already exist.

If anything fails part-way, the agent calls `release_source(path)` and the file returns to
`raw/`. If an agent crashes or simply stops, the server returns the file to `raw/` once the lease
expires (it checks every 60 seconds), so another agent can try again. Only the agent holding the
lease can complete or release it (`not_claimed` otherwise).

To ingest by hand with your own agent, install the `docs-ingest` skill
([CONNECT-AGENTS.md](CONNECT-AGENTS.md#install-the-kits-skills)) and ask: *"Use the docs-ingest
skill to ingest the next source in the docs vault."*

## Prompt-injection safety

Imported documents are untrusted. A web page, chat export or PDF can contain text written to
steer an AI ("ignore your instructions and…"). The kit's defences:

- **The rules say so.** `AGENTS.md` rule 1, the `docs-ingest` skill, the URL stub text and the
  agent-runner prompt all tell agents to treat source content as data, never as instructions.
  This depends on the model following it; it is not a guarantee.
- **The server does the dangerous parts itself, or not at all.** It never fetches URLs. It refuses
  writes outside the token's scope, to protected rule files, to `read.deny` folders, to recently
  saved pages, and anything that looks like a secret. Human pages follow the admin's mode.
  There is no delete tool. A hijacked agent can still make bad edits to allowed human docs and
  wiki pages; history, audit and notices aid recovery but do not prove an edit correct.
- **Review queue items can't smuggle links.** `review_queue_add` flattens each field to one line and
  strips `[[` / `]]`, so an injected item can't break the task list or add links.
- **agent-runner is boxed in.** It may only use the vault tools (`mcp__vault__*`) and Claude
  Code's `WebFetch`: no shell and no file access around the policy. `WebFetch` is there so it can
  fetch URL stubs. An injected instruction could still make it request an arbitrary URL, and put
  vault text it can read into that URL. See [SECURITY.md](SECURITY.md) for the threat model.

What you can do:

- Give agents that only answer questions a `--scope read` token.
- Give ingest agents a write token limited to the folders they need, e.g.
  `scripts/token create --name ingest-bot --owner you@example.com --scope write --folders wiki/,raw/,_system/,domains/,shared/`.
  (`review_queue_add` still works with folder-limited tokens.)
- Don't ingest sources with the same assistant session that has access to your email, chat or
  other write-capable tools; an injected instruction could reach those tools.
- Skim new `wiki/sources/` summaries from untrusted origins. To undo an agent's edit, an admin
  can restore the previous version with `scripts/history` (see the
  [user guide](USER-GUIDE.md#page-history-and-restoring-a-version)).
