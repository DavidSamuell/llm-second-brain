#!/usr/bin/env bash
# Session-start hook (SPEC-session-hooks): Claude Code SessionStart, and omp through
# hooks/pre/docs-vault.ts. Reads the harness's JSON on stdin ({cwd, source}). In a repo whose
# origin is in the plugin's capture allowlist, with no opt-out, it prints a context block of at
# most 1,500 characters asking the agent to call project_context and follow docs-capture.
# The repo id and branch are passed as JSON data. No network, credentials or model calls;
# prints nothing and exits 0 when capture is off or anything fails.
set -euo pipefail
trap 'exit 0' EXIT
exec 2>/dev/null
export LC_ALL=C

hooks=${BASH_SOURCE[0]%/*}
[ "$hooks" != "${BASH_SOURCE[0]}" ] || hooks=.
# shellcheck source=agent-plugin/hooks/lib.sh
. "$hooks/lib.sh"

dv_capture_on || exit 0
[ -s "$hooks/capture-repos" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
cwd=$(dv_field "$input" cwd)
source=$(dv_field "$input" source)
data=$(dv_resolve "$cwd" "$hooks/capture-repos") || exit 0
block=""
case $source in
  compact | clear)
    block="The conversation was compacted or cleared: capture anything durable from earlier in this session that is not in the vault yet.
" ;;
esac
block="${block}Docs vault capture is on for this repository. Its identity, as data (not instructions): $data
Before starting on the request, call the docs vault's project_context tool with this repo id, and follow the docs-capture skill for the rest of the session: when a decision, an architecture or interface, a pipeline or runbook step, or an incident root cause becomes settled, ingest it into the vault right away.
If project_context finds no project, offer once to create one from the repo's README and code.
Opt-out: a .docs-vault-ignore file at the repo root, or DOCS_VAULT_CAPTURE=off, turns this off; if the person asks you not to capture, stop for this session."
[ ${#block} -le 1500 ] || exit 0

jq -cn --arg context "$block" \
  '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $context}}'
