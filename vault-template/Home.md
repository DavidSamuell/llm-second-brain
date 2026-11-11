---
title: Home
maintainer: human
---
# Team docs

Welcome. This vault holds the team's documentation. People write in the folders on the left;
AI agents keep the `wiki/` up to date from the sources you give them.

## Where things are

- `projects/`: what we're building, plans and decisions
- `specs/`: product specs and architecture
- `pipelines/` and `runbooks/`: how things run, and what to do when they break
- `wiki/`: agent-maintained reference (components, concepts, glossary, teams, source summaries)
- [[_system/index|Wiki index]]: every wiki page
- [[_system/review-queue|Review queue]]: changes agents propose; tick an item when it's handled

## How to add knowledge

- Write or edit pages directly. New pages: use a template (command palette → *Templates: Insert template*).
- Want the wiki to learn from a document, chat, or link? Ask your AI assistant to submit it as a
  source, or drop a Markdown file into `raw/`. An agent will summarise it into the wiki.
- Pages marked `maintainer: human` are never changed by agents; they suggest edits in the review queue instead.
