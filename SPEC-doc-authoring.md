# Spec: doc-authoring

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Implemented; Checkpoint K ready for review (criteria 1–4 verified, live criterion 5 at T59) · **Depends on:** write-policy, knowledge-graph

## Objective

Agents write and maintain the documentation people read, and keep the wiki as the connected map behind it:

| Folder | Audience | Content | Kept by |
|---|---|---|---|
| `domains/<domain>/README.md` | people | One overview per domain (a product area such as `asr`): what it covers and its projects | people (agents only with the person's agreement) |
| `domains/<domain>/<project>/README.md` | people | One overview per project: goal, status, team, repos, key decisions, links to its specs, pipelines and runbooks | agents (people may edit) |
| `domains/<domain>/<project>/specs/`, `decisions/` | people | Product specs, architecture docs and ADRs for the project | agents |
| `domains/<domain>/<project>/pipelines/` | people | Data, training and build pipelines: inputs, steps, outputs, where they run | agents |
| `domains/<domain>/<project>/runbooks/` | people | How to operate and fix things | agents |
| `domains/<domain>/shared/…`, `shared/…` | people | The same four kinds of doc when they serve a whole domain, or several domains | agents |
| `wiki/` | agents | Atomic reference pages (components, concepts, glossary, teams, source summaries) with typed edges; links to the human docs instead of repeating them | agents |

When new information arrives, agents update the affected human docs directly (history keeps every version) and record what changed.

**Users:** team members reading docs; their agents and the nightly agent writing them.

## Behaviour

### Where things go (AGENTS.md, schema_version 4)

Schema 2 and 3 kept human docs in flat `projects/ specs/ pipelines/ runbooks/` folders. Schema 4 groups them by domain, then project:

```
domains/<domain>/README.md                              type: domain   (domain template)
domains/<domain>/<project>/README.md                    type: project  (project-overview template)
domains/<domain>/<project>/specs/<name>.md              product-spec, architecture
domains/<domain>/<project>/decisions/NNNN-<title>.md    adr
domains/<domain>/<project>/pipelines/<name>.md          pipeline
domains/<domain>/<project>/runbooks/<name>.md           runbook, guide
domains/<domain>/shared/{specs,decisions,pipelines,runbooks}/   docs for the whole domain
shared/{specs,decisions,pipelines,runbooks}/            docs spanning several domains
```

- Domain and project folder names are lowercase kebab-case. `shared` is reserved and is never a project name.
- The project overview (`domains/asr/parakeet/README.md`, from `project-overview`):
  - frontmatter: `type: project`, `maintainer: shared`, `repo: [github.com/org/name, …]`, `status`, `owners`, `graph`;
  - body sections: Summary, Status, Team, Repositories, Specs, Pipelines, Runbooks, Decisions, Change log.
- Overview files are always `README.md`, so links to them use the full path: `[[domains/asr/parakeet/README]]`. Node ids follow the usual rule, `<node type>:<path without .md>`: `project:domains/asr/parakeet/README`, `domain:domains/asr/README`.
- Every other doc has `project:` frontmatter listing the projects it belongs to, as full-path wikilinks: `project: ["[[domains/asr/parakeet/README]]"]`. A shared doc lists every project it serves, or `[]` when it serves a whole domain.
- The `project:` list decides the folder: one project → `domains/<d>/<project>/<subfolder>/`; several projects of one domain (or `[]`) → `domains/<d>/shared/<subfolder>/`; projects in several domains → `shared/<subfolder>/`. A project is added only when a source or the person shows it uses the whole doc; the agent then `move`s the doc to match in the same session. Partial overlap: the doc stays and the other project's `README.md` links the section. `docs-lint` reports misplaced docs and moves them (review item if the move is refused).
- Every human doc has a `graph` node. The matching wiki page carries `documented_in` to it (a domain overview may also be the target), and the human doc carries `belongs_to_project`.

### Domains

- People create domains. An agent creates `domains/<new>/README.md` only after the person in the session agrees.
- When a new project fits no existing domain:
  - in a session, the agent asks the person to pick an existing domain or approve a new one;
  - unattended (nightly maintenance, no person), the agent still writes the source summary, wiki pages and graph edges, skips the project's human docs, adds one review item "Project <x> needs a domain: suggest <y>", and completes the source.

### Writing rules

- Write for a new team member: plain language, short sections, current state first. No agent working notes.
- Every fact cites a source-summary page or a repo location (`repo@commit:path`).
- **Repos:** agents may read a repo's README and code and submit a repo summary (`submit_source`, origin `repo:<remote>@<commit>`). That summary is ingested like any source.
- **No secrets:** never copy secrets, credentials or `.env` contents. Code excerpts only when short and necessary.
- **Updating:**
  - change the affected sections only (`replace_section`/`insert_after`);
  - replace obsolete statements in place rather than piling up history;
  - add one dated line to `## Change log`: `- 2026-11-17: Training now runs on GPU2 (was GPU1) — [[wiki/sources/…]]`.
- **Contradictions:** newer, better-sourced information becomes the current text. The older claim stays visible in the change log and as a typed edge with `status: superseded`. If the agent can't tell which source is right, it keeps the current text and files a review item.
- **Edits by hand:** pages a person marked `maintainer: human` follow write-policy (edit with a summary; a notice is raised).
- **Wiki pages** stay atomic: facts, typed edges and links. They never copy the narrative of a human doc; they link with `documented_in`.

### Skills (shipped in the template and the agent plugin)

- `docs-ingest`: after the wiki step, update or create the affected human docs, then the index and log.
- `docs-query`: start from the project's graph neighbourhood (knowledge-graph tools), then read the human docs and wiki pages; cite pages.
- `docs-capture` (new): ingest durable findings from the current work at the moment they happen, completely (submit, claim, summary, human docs, wiki, graph, complete). That covers decisions, architecture, interfaces, pipeline or runbook steps, incidents and conventions. It never records transient debugging, guesses, personal data or secrets.
- `docs-lint`: adds checks that human docs match the wiki, plus graph lint.

### Commands (in the agent plugin)

Plain requests ("create pipeline docs for this project and put them in the vault") trigger the same skills. The commands are a reliable shortcut. Each acts **immediately** in the session; nothing waits for the nightly run.

| Command | Does |
|---|---|
| `/docs-vault:document <what>` | Writes or updates a human doc from the current repo and conversation (e.g. `pipeline docs for this project`, `runbook for the nightly backup`). It picks the folder and template, reads the relevant repo files, cites `repo@commit:path`, links the doc from the project page (creating the project page if missing, in a domain the person picks or approves), adds the wiki entry and `documented_in` edge, and logs it. The repo reading is submitted and ingested as a source in the same step. |
| `/docs-vault:ingest <file, URL or text>` | Submits the source and ingests it now (claim → summary → human docs and wiki → complete). For a URL, the agent fetches it with its own tools first. |
| `/docs-vault:ask <question>` | Answers from the vault only (`project_context`, graph, search), citing pages, and says what the vault doesn't cover. |
| `/docs-vault:lint [folder]` | Runs the structural and semantic checks on demand and fixes what it may, like the nightly run but scoped. |
| `/docs-vault:connect` | Checks the connection and sign-in (exists). |

- **Claude Code:** from the plugin's `commands/` folder, as `/docs-vault:<name>`.
- **omp:** loads Claude plugin commands under the same names.
- **Codex:** plugins carry skills only, so the commands aren't available; invoke the skills (`$docs-ingest` …) or ask in plain words.

### MCP tool

- `project_context(repo?, project?)`, read scope. It returns:
  - the project page (summary and status sections);
  - its 1-hop graph neighbourhood (titles, paths, edge types);
  - open review items for those pages;
  - the 5 most recent change-log lines.

  It stays within 4,000 characters and is permission-filtered. `repo` is a normalised git remote (`github.com/org/name`, from https or ssh forms). Unknown repo → `{project: null}`.

  Projects are found only at `domains/*/*/README.md` with `type: project` (exactly two levels under `domains/`). `project` matches the path, the path without `.md`, the project folder name (`parakeet`), the title, the node id (`project:domains/asr/parakeet/README`) and graph aliases. The file stem `README` is never a selector.

  Context is a bounded preview, not an exhaustive project export: long text is shortened,
  at most three readable open review items are included, and only whole graph records that fit
  are returned. Use `read` and the graph tools for complete detail. If both selectors are given,
  they must identify the same readable project; ambiguous matches also return `{project: null}`.

## Commands

```bash
uv run pytest vault-mcp -q -k "project_context or repo"
uv run pytest e2e -k "doc_authoring" -m "profile_local or profile_none"
scripts/upgrade-vault            # brings an existing vault's AGENTS.md, templates and skills to the current schema
```

Schema 4 changes the layout, but `scripts/upgrade-vault` only updates the managed rule files. A vault with pages in the old flat folders must move them by hand with the `move` tool; `scripts/history` keeps their earlier versions under the old paths.

## Project structure

- `vault-template/AGENTS.md` (§2 layout, §5 conventions, §9 ingest, §10 query, a new capture section), `vault-template/_system/templates/*` (`repo`, `project`, `Change log`), `vault-template/skills/{docs-ingest,docs-query,docs-lint,docs-capture}/SKILL.md`.
- `vault-mcp/src/vault_mcp/tools_project.py`: `project_context`; `vault-core` repo-URL normalisation.
- The agent plugin picks up the new skill through `scripts/agent-plugin` (no generator change beyond copying it).
- `docs/USER-GUIDE.md` (what people will find where), `docs/IMPORTING.md`.

## Code style

```python
def normalise_remote(url: str) -> str | None:
    """`git@github.com:Org/Repo.git`, `https://github.com/org/repo` -> `github.com/org/repo`."""
```

Skill text: imperative steps, tool names without harness prefixes, as in the existing skills.

## Testing strategy

- **Unit:** remote normalisation (ssh, https, `.git` suffix, case, trailing slash, non-GitHub hosts); `project_context` size cap, permission filtering, and an unknown repo.
- **e2e (scripted agent, as the agent-runner tests do):** a source describing a new pipeline for project `parakeet` in domain `asr` is ingested. Expected:
  - `domains/asr/parakeet/pipelines/<name>.md` is created from the template, with a citation;
  - `domains/asr/parakeet/README.md` links it;
  - the wiki page has `documented_in`.

  A second source changing one step updates that section only and adds one change-log line, and history shows both versions.
- **Generator:** the plugin contains the four new commands, and `claude plugin validate` passes.
- **e2e (scripted agent):** `document` in a sample repo creates `domains/<domain>/<project>/pipelines/<name>.md` with repo citations, a project link and a wiki `documented_in` edge; `ingest` of pasted text leaves the source in `raw/_processed/` with its summary, in the same run.
- **Manual (live):** in Claude Code and omp, `/docs-vault:document pipeline docs for this project` in a Bookbot repo, reviewed by a person for readability; one plain-language request for the same thing.

## Boundaries

- **Always:** cite every fact; keep human docs readable without the wiki; one change-log line per update.
- **Ask first:** new domains, new top-level folders, or changing a folder's purpose.
- **Never:** paste secrets or large code; delete pages (retire them with `status: archived`); put agent working notes in human docs.

## Success criteria

1. The e2e scenario above passes.
2. `project_context(repo="git@github.com:bookbot-hive/foo.git")` returns the project whose `repo:` lists `github.com/bookbot-hive/foo`, within 4,000 characters.
3. `docs-lint` reports a human doc whose cited source has been superseded.
4. An existing vault upgraded with `scripts/upgrade-vault` gets schema 2 rules, templates and skills, without touching custom blocks.
5. `/docs-vault:document`, `ingest`, `ask` and `lint` work in Claude Code and omp and finish within the session; nothing they produce is left in `raw/` for the nightly run.

## Open questions

- None blocking.
