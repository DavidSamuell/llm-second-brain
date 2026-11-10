#!/usr/bin/env bash
# agent-runner entrypoint (SPEC §10.2, plan D30).
#
#   agent-runner cron            run INGEST_CRON / LINT_CRON jobs with supercronic (default)
#   agent-runner run-job <job>   run one job now (ingest | lint)
set -euo pipefail

STATE=${DATA_DIR:-/data}/agent-runner
export ALERT_SERVICE=agent-runner ALERT_STATE_DIR=$STATE/alerts
mkdir -p "$STATE" "$HOME/.claude/skills"
: "${VAULT_MCP_TOKEN:?AGENT_RUNNER_TOKEN is not set (run scripts/setup)}"
export VAULT_MCP_URL=${VAULT_MCP_URL:-http://vault-mcp:8082/mcp}

# Skills: the vault's own (mounted read-only) win over the kit's built-in copies.
for dir in /opt/kit-skills /vault-skills; do
  [ -d "$dir" ] || continue
  for skill in "$dir"/*/; do
    [ -f "$skill/SKILL.md" ] || continue
    rm -rf "$HOME/.claude/skills/$(basename "$skill")"
    cp -r "$skill" "$HOME/.claude/skills/"
  done
done

# vault-mcp connection for Claude Code; the token stays in this private file.
umask 077
jq -n --arg url "$VAULT_MCP_URL" --arg token "$VAULT_MCP_TOKEN" \
  '{mcpServers: {vault: {type: "http", url: $url, headers: {Authorization: ("Bearer " + $token)}}}}' \
  > "$STATE/mcp.json"
umask 022

if [ -n "${AGENT_COMMAND:-}" ]; then
  echo "agent-runner: custom AGENT_COMMAND; the monthly budget is enforced only if it prints a JSON line with total_cost_usd" >&2
elif [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "agent-runner: ANTHROPIC_API_KEY is not set; jobs will fail" >&2
fi
[ -n "${AGENT_MONTHLY_BUDGET:-}" ] || echo "agent-runner: AGENT_MONTHLY_BUDGET is empty: spending is not capped" >&2

case "${1:-cron}" in
  cron)
    crontab=$STATE/crontab
    {
      echo "${INGEST_CRON:-0 * * * *} run-job ingest"
      echo "${LINT_CRON:-0 2 * * *} run-job lint"
      echo "* * * * * touch $STATE/heartbeat"
    } > "$crontab"
    touch "$STATE/heartbeat"
    echo "agent-runner: ingest '${INGEST_CRON:-0 * * * *}', lint '${LINT_CRON:-0 2 * * *}' (UTC)"
    exec supercronic -passthrough-logs "$crontab"
    ;;
  run-job) shift; exec run-job "$@" ;;
  *) exec "$@" ;;
esac
