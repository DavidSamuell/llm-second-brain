# Spec: session-hooks

**Module of:** [SPEC.md §18](SPEC.md#18-v02-second-brain-capability-map) · **Status:** Draft · **Depends on:** doc-authoring (and through it write-policy, knowledge-graph)

## Objective

When a team member opens an agent session in a project repo with the docs-vault plugin installed, the agent:

1. starts knowing the project: what the vault says about it, related docs, and open review items;
2. records durable findings into the vault **as they happen during the session**, not only at the end;
3. writes a repo summary and project page the first time it meets a repo the vault doesn't know.

It works like llm-wiki-database's always-on behaviour, but against the shared vault through vault-mcp. Harnesses: **Claude Code and omp** are required. **Codex** is included if the plan's spike confirms its plugin-bundled hooks work in the CLI. Its docs (2026-11) describe plugin hooks with `SessionStart` and `additionalContext`, behind a one-time `/hooks` trust review.

**Users:** Bookbot team members working in project repos.

## Behaviour

### Where capture runs

- **Repos:** only repos whose normalised remote matches the allowlist baked into the plugin: `scripts/agent-plugin --capture-repos 'github.com/bookbot-hive/*,github.com/bookbotkids/*'`. Other repos, and folders without a git remote, get no injection and no capture.
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
  - an incident root cause.
- Each finding: `search` or `project_context` to dedupe, then `submit_source` (tagged with the project and repo@commit) and, when clear-cut, a direct update of the human doc (doc-authoring rules).
- **Turn-end nudge** (Claude Code `Stop`, omp's equivalent, Codex `Stop`): at most once every 10 turns per session, the hook adds a one-line reminder to capture anything durable so far. It never blocks the turn and never forces a continuation.

### Before compaction and at session end

- On `PreCompact` (or the harness's equivalent) and on session end, inject a reminder to capture outstanding findings. If the harness can't run a model step at session end (Claude Code's `SessionEnd` can't), the compaction and turn-end reminders are the safety net; nothing more runs after the session closes.
- The nightly maintenance run ingests whatever sources were submitted (scheduled-maintenance).

### Cost

- Hooks add no model calls; capture uses the session's own model, bounded by the skill's rules.
- Target: under 5 vault tool calls at session start, and one call per durable finding.

## Commands

```bash
scripts/agent-plugin --capture-repos 'github.com/bookbot-hive/*,github.com/bookbotkids/*'
uv run pytest e2e/test_agent_plugin.py -q          # generator output: hooks per harness, allowlist baked in
bash agent-plugin-dist/docs-vault/hooks/session-start.sh < fixture.json   # hook unit run
```

## Project structure

- `agent-plugin/hooks/`:
  - `session-start.sh` (repo detection, allowlist, opt-out, context block);
  - `turn-end.sh` (every-10-turns nudge, state in the harness's session temp dir);
  - per-harness wiring: Claude Code `hooks/hooks.json`, Codex `hooks/hooks.json` under its plugin rules, and omp, whatever the spike shows works: an extension module in the plugin package, or omp reading Claude plugin hooks.
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
- **Spike (plan, first task):** in each harness, confirm that a plugin-bundled `SessionStart` injects context and that turn-end/pre-compact events fire (omp: extension vs Claude-hook support; Codex: CLI plugin hooks after `/hooks` trust).
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
2. A decision stated mid-session is captured, as a source or a human-doc update, before the session ends, without the user asking.
3. A repo the vault doesn't know gets a project page and repo summary after the user agrees.
4. Non-allowlisted repos, `.docs-vault-ignore` and `DOCS_VAULT_CAPTURE=off` produce no injection and no vault calls.
5. A hook failure (e.g. no `jq`, no git) leaves the session working normally.
6. Codex: the same as 1–2 if the spike passes; otherwise documented as not supported in v0.2.

## Open questions

- The capture allowlist for Bookbot: `github.com/bookbot-hive/*` and `github.com/bookbotkids/*`? (Assumed in the commands above.)
