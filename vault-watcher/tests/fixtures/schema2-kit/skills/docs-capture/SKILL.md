---
name: docs-capture
description: Record a durable finding from the current work in the team docs vault the moment it becomes durable - a decision, an architecture or interface clarified, a pipeline or runbook step worked out, an incident root cause, a team convention - and ingest it completely in this session. Use while working in a repo connected to the docs vault, or when asked to document, save or capture something in the vault.
---
# docs-capture

Keep the team docs current while you work. When a finding becomes durable, write it up as a
source and ingest it straight away with the `docs-ingest` skill: submit, claim, source summary,
human docs, wiki and graph, index and log, complete. Don't batch findings for the end of the
session, and never leave them in `raw/` for a later run.

## What to capture

Capture, once each is settled:

- a decision made (what, why, alternatives rejected, who decided);
- an architecture, interface, schema or configuration clarified;
- a pipeline or runbook step worked out (commands, inputs, outputs, where it runs);
- an incident's confirmed root cause and fix;
- a team convention agreed.

Never capture: transient debugging, guesses or unconfirmed hypotheses, personal data, secrets,
credentials, `.env` contents, raw logs, large code, or facts the vault already states. If the
person asks you not to capture, stop for this session.

## Steps (vault-mcp)

1. Once per session: `get_schema`, and `project_context(repo=<repo id>)` for the repo you work
   in. The repo id is `git remote get-url origin` (any form; the server normalises it, e.g.
   `github.com/org/app`). No project returned: offer once to create one from the repo's README
   and code, and continue if the person agrees or doesn't object.
2. **Dedupe:** `search` (two phrasings) and `graph_neighbors` around the project or component.
   Already stated the same way: stop. Stated differently: this is an update; capture what changed
   and what it replaces.
3. **Write the source** as short factual notes, one finding per source:
   - repo facts cite `repo@commit:path` with the commit from `git rev-parse HEAD`
     (e.g. `github.com/org/app@3f2c9e1:train/config.yaml`). Cite committed code only; if a
     finding depends on uncommitted changes, capture it after they are committed;
   - decisions from the conversation say who decided and when;
   - web content: fetch it with your own tools and include the content, not just the URL.
4. `submit_source(title, origin, content)` with origin `repo:<repo id>@<commit>` for repo
   reading, or `capture:<repo id>@<commit>` for a session finding. Keep the returned `raw/…`
   path.
5. Follow the `docs-ingest` skill from its claim step with that path, to the end
   (`complete_source`). The human docs it updates get a change-log line and citations; the wiki
   pages get typed edges with evidence and `documented_in` links.
6. Tell the person in one line what you saved and where (source summary and docs touched).

If a step fails, `docs-ingest` releases the source (`release_source`); tell the person which
pages were already written and that the source is back in `raw/` for another agent. Don't claim
a rollback.

## Local clone

Same steps; write the source file into `raw/` yourself, then follow the `docs-ingest` local-clone
steps and push.
