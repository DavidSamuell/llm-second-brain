---
title: Home
maintainer: human
---
# Team docs

Welcome. This vault holds the team's documentation. People and their AI agents write the docs in
`domains/` and `shared/`; agents also keep the `wiki/` behind them up to date from the sources you
give them.

## Where things are

- `domains/<domain>/`: one folder per area of work (e.g. `asr`), with a `README.md` overview
- `domains/<domain>/<project>/`: a project's `README.md` (summary, status, team, repositories,
  change log) and its `specs/`, `decisions/`, `pipelines/` and `runbooks/`
- `domains/<domain>/shared/` and `shared/`: docs used by several projects of a domain, or by
  several domains
- `wiki/`: agent-maintained reference (components, concepts, glossary, teams, source summaries)
- [[_system/index|Wiki index]]: every wiki page
- [[_system/review-queue|Review queue]]: changes agents propose; tick an item when it's handled

## How to add knowledge

- Write or edit pages directly. New pages: use a template (command palette → *Templates: Insert template*).
- Want the docs to learn from a document, chat, repository or link? Ask your AI assistant to
  ingest or document it; it does so in the same session. You can also drop a Markdown file into
  `raw/` for an agent to ingest later.
- Every change is kept in history. Agent edits to pages marked `maintainer: human` follow the
  vault's policy: either agents only suggest changes in the review queue, or each edit raises a
  review notice.
