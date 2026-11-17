#!/usr/bin/env bash
# Prompt hook (SPEC-session-hooks): Claude Code UserPromptSubmit, and omp through
# hooks/pre/docs-vault.ts. Reads the harness's JSON on stdin ({session_id, cwd}). Checks the
# allowlist and both opt-outs on every prompt, counts prompts per session, and on every 10th adds
# a one-line reminder to capture durable findings. Never blocks a prompt. No network, credentials
# or model calls; prints nothing and exits 0 when capture is off or anything fails.
set -euo pipefail
trap 'exit 0' EXIT
exec 2>/dev/null
export LC_ALL=C

hooks=${BASH_SOURCE[0]%/*}
[ "$hooks" != "${BASH_SOURCE[0]}" ] || hooks=.
# shellcheck source=agent-plugin/hooks/lib.sh
. "$hooks/lib.sh"

every=10

dv_capture_on || exit 0
[ -s "$hooks/capture-repos" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
cwd=$(dv_field "$input" cwd)
session=$(dv_field "$input" session_id)
[ -n "$session" ] || exit 0
dv_resolve "$cwd" "$hooks/capture-repos" >/dev/null || exit 0

count=$(dv_next_count "$session") || exit 0
[ $((count % every)) -eq 0 ] || exit 0

jq -cn --arg context "Docs vault reminder: if anything durable was settled in this session so far (a decision, an architecture or interface, a pipeline or runbook step, an incident root cause), capture it now with the docs-capture skill." \
  '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $context}}'
