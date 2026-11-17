---
description: Answer a question from the team docs vault only, with citations (read-only)
argument-hint: <question>
---
Answer from the team docs vault only: $ARGUMENTS

Use the `docs-query` skill. This command is strictly read-only: use only `get_schema`,
`project_context`, `graph_neighbors`, `graph_edges`, `graph_path`, `backlinks`, `search`,
`list` and `read`. Never call `write`, `append`, `replace_section`, `insert_after`, `move`,
`submit_source`, `claim_source`, `complete_source`, `release_source` or `review_queue_add`, and
don't edit files.

1. In a repo, start with `project_context(repo=<git remote get-url origin>)`.
2. Follow the graph and search, read the best pages, and answer briefly, citing every claim with
   its page (`[[pipelines/asr-training]]`).
3. Say what the vault doesn't cover; don't fill gaps from general knowledge as vault facts.
4. If the answer is worth keeping, say so and suggest `/docs-vault:ingest` or
   `/docs-vault:document`; don't save it yourself.
