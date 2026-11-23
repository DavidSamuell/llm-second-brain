# Spec: session-hooks

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Released in v0.2.0; all criteria verified (1–3 live in Claude Code and omp, T59) · **Depends on:** doc-authoring (and through it write-policy, knowledge-graph)

## Objective

When a team member opens an agent session in a project repo with the docs-vault plugin installed, the agent:

1. starts knowing the project: what the vault says about it, related docs, and open review items;
2. ingests durable findings into the vault **as they happen during the session**, fully, not only at the end and not queued for later (the same model as llm-wiki-database);
3. writes a repo summary and project page the first time it meets a repo the vault doesn't know.

It works like llm-wiki-database's always-on behaviour, but against the shared vault through vault-mcp. Harnesses: **Claude Code and omp.** **Codex is out for v0.2:** the T42 spike showed Codex CLI 0.167–0.169 ignores plugin-bundled hooks (supported only in Codex desktop). The plugin still gives Codex the MCP server and skills.

**Users:** Bookbot team members working in project repos.

## Behaviour

### Where capture runs

- **Repos:** only repos whose normalised remote matches the allowlist baked into the plugin: `scripts/agent-plugin --capture-repos 'github.com/bookbot-hive/*,github.com/bookbot-kids/*'`. Other repos, and folders without a git remote, get no injection and no capture.
- **Opting out:**
  - per repo: a `.docs-vault-ignore` file at the repo root;
  - per session: `DOCS_VAULT_CAPTURE=off`.

  Hooks then do nothing.

### Session start (hook)

- A small hook script shipped in the plugin (bash, no network, under 1 s) works out the repo, its branch and whether capture is allowed. It injects a context block of at most 1,500 characters:
  - the repo id;
  - the instruction "call the vault's `project_context` tool with this repo before starting, and follow the `docs-capture` skill during this session";
  - the opt-out note.
- The **agent** calls the vault itself, with its own OAuth sign-in. Hook scripts never hold or read vault credentials, and make no model calls.
- If `project_context` returns no project, the agent offers once to create one from the repo README and code (doc-authoring rules), and proceeds if the person agrees or doesn't object.

### During the session

- The injected instruction and the `docs-capture` skill make the agent record a finding when it becomes durable:
  - a decision made;
  - an architecture or interface clarified;
  - a pipeline or runbook step worked out;
  - an incident root cause;
  - in ML work: dataset versions, splits and normalisation rules, final evaluation results with their tracker run link, the eval protocol and baselines, negative results, training gotchas, error-analysis conclusions, model lineage, promotion criteria and export accuracy loss.
- Each finding is ingested completely, right then (`docs-capture` skill):
  1. `search` or `project_context` to dedupe;
  2. `submit_source` (tagged with the project and repo@commit), then `claim_source`;
  3. write the source summary, update or create the affected human docs and wiki pages with typed edges (doc-authoring rules), and add the index and log entries;
  4. `complete_source`.

  If a step fails, `release_source` hands the source back, and the nightly run picks it up.
- **Prompt nudge** (Claude Code `UserPromptSubmit`, omp `before_agent_start`): on every 10th prompt of a session, the hook adds a one-line reminder to capture anything durable so far. It never blocks a prompt. (`Stop` isn't used: it can't add context without forcing a continuation.)

### Before compaction and at session end

- After compaction the context block is injected again (Claude Code fires `SessionStart` with `source: compact`; omp re-injects after `session_compact`/`session_clear`), with a reminder to capture anything outstanding. Nothing runs after a session closes (no model step is possible then); the prompt nudges are the safety net.
- The nightly maintenance run is only a safety net. It ingests sources nobody finished: files dropped in through the editor, released sources, and sessions that ended mid-ingest (scheduled-maintenance).

### Cost

- Hooks add no model calls; capture uses the session's own model, bounded by the skill's rules.
- Target: under 5 vault tool calls at session start; a full ingest per durable finding, typically 6–12 vault calls.

## Commands

```bash
scripts/agent-plugin --capture-repos 'github.com/bookbot-hive/*,github.com/bookbot-kids/*'
uv run pytest e2e/test_agent_plugin.py -q          # generator output: hooks per harness, allowlist baked in
bash agent-plugin-dist/docs-vault/hooks/session-start.sh < fixture.json   # hook unit run
```

## Project structure

- `agent-plugin/hooks/`:
  - `session-start.sh` (repo detection, allowlist, opt-out, context block);
  - `prompt-nudge.sh` (every 10th prompt, state per session id in `$TMPDIR`);
  - wiring: Claude Code `hooks/hooks.json` (`SessionStart`, `UserPromptSubmit`); omp `hooks/pre/docs-vault.ts` (calls the same scripts; `before_agent_start`).
- `scripts/agent-plugin`: copies hooks and wiring, and bakes in the allowlist.
- `docs/CONNECT-AGENTS.md` (what the plugin does automatically, opt-out, Codex hook trust), and the plugin repo README.

## Code style

Hook scripts are POSIX-friendly bash, `set -euo pipefail`, and print JSON on stdout as each harness expects. They always exit 0, so a failing hook never breaks a session.

```bash
remote=$(git -C "$cwd" remote get-url origin 2>/dev/null) || exit 0
repo=$(normalise_remote "$remote") || exit 0
allowed "$repo" || exit 0
[ -e "$root/.docs-vault-ignore" ] || [ "${DOCS_VAULT_CAPTURE:-on}" = off ] && exit 0
jq -cn --arg c "$(context_block "$repo")" '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$c}}'
```

## Testing strategy

- **Unit (bats or pytest with subprocess):** each hook script against fixture inputs per harness:
  - allowed vs not-allowed repo;
  - ssh and https remotes;
  - no remote;
  - opt-out file and env;
  - nudge cadence;
  - output schema per harness;
  - the 1,500-character cap.
- **Generator:** `scripts/agent-plugin` output contains the hooks for each harness and the baked allowlist, and still validates (`claude plugin validate`).
- **Spike (T42, done):** results in `tasks/impl-notes.md`.
- **Manual (live, Bookbot):** in Claude Code and omp, open a session in a bookbot-hive repo:
  - the agent calls `project_context` first;
  - a decision made mid-session appears in the vault within that session (audit shows the person's grant);
  - a non-allowlisted repo gets nothing.

## Boundaries

- **Always:** fail open (a hook error means no injection, never a broken session); respect the allowlist and the opt-outs; keep credentials out of hook scripts.
- **Ask first:** capturing in repos outside the allowlist, or adding model calls inside hooks.
- **Never:** send repo content to the vault from a hook script directly; block or force-continue a turn; capture secrets or personal data.

## Success criteria

1. In Claude Code and omp, opening a session in an allowlisted repo makes the agent call `project_context` before its first answer (audit line under the user's grant).
2. A decision stated mid-session is fully ingested before the next answer finishes, without the user asking: the source is in `raw/_processed/`, the human doc and wiki page are updated, and graph edges are present.
3. A repo the vault doesn't know gets a project page and repo summary after the user agrees.
4. Non-allowlisted repos, `.docs-vault-ignore` and `DOCS_VAULT_CAPTURE=off` produce no injection and no vault calls.
5. A hook failure (e.g. no `jq`, no git) leaves the session working normally.
6. Codex: documented as not supported for automatic behaviour in v0.2 (CONNECT-AGENTS and the plugin README); its MCP server and skills still work.

## Open questions

- None. Bookbot's capture allowlist is `github.com/bookbot-hive/*,github.com/bookbot-kids/*` (confirmed 2026-11-17).
