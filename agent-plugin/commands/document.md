---
description: Write or update a team doc (project, spec, pipeline, runbook) in the docs vault from this repo and conversation, now
argument-hint: <what to document, e.g. "pipeline docs for this project">
---
Document this in the team docs vault now: $ARGUMENTS

Use the `docs-capture` skill, which hands over to the `docs-ingest` skill. Finish in this
session; leave nothing in `raw/`.

1. `get_schema`. Work out the repo id (`git remote get-url origin`) and commit
   (`git rev-parse HEAD`), then `project_context(repo=<repo id>)`.
2. Read the repo files the request needs (README, code, configs, CI). Skip `.env`, credentials
   and generated files; never copy secrets; short excerpts only.
3. Write one source with the facts the doc needs, each cited `repo@commit:path`, plus anything
   settled in this conversation. `submit_source(title, origin="repo:<repo id>@<commit>",
   content)`.
4. Ingest that source now with `docs-ingest`, from its claim step to `complete_source`. Make
   sure it creates or updates the requested doc in the right folder from its template, links it
   from the project page (creating the project page if missing), adds the wiki entry with a
   `documented_in` edge, a change-log line, and the index and log entries.
5. Reply with the doc path, the source summary path and a one-line summary of what changed. If
   something failed, say which pages were written and that the source was released.
