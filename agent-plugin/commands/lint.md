---
description: Check the team docs vault (or one folder) now and fix what agents may fix
argument-hint: "[folder, e.g. pipelines/]"
---
Lint the team docs vault now. Scope: $ARGUMENTS (empty means the whole vault).

Use the `docs-lint` skill, limited to that folder when one is given: structural checks, human
docs against the wiki, human docs citing superseded sources, and `graph_lint`. Fix what you may
fix, add one review item for everything else, then write `_system/lint-report.md` and the log
line.

Reply with the counts per check, what you fixed, and the review items you added.
