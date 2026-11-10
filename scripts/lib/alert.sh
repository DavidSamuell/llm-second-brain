#!/usr/bin/env bash
# Webhook alerts for the kit's shell services (SPEC §12, plan D25); same payloads as
# vault_core.alerts: Slack {"text"}, Discord {"content"}, otherwise generic JSON.
#
#   source scripts/lib/alert.sh
#   send_alert <event> <severity> <message> [<subject>]
#
# ALERT_WEBHOOK unset: logs only. The same event+subject is sent at most once an hour
# (ALERT_STATE_DIR keeps the timestamps). Delivery failures are logged and never fatal.

ALERT_DEDUPE_S=${ALERT_DEDUPE_S:-3600}

send_alert() {
  local event=$1 severity=$2 message=$3 subject=${4:-} service=${ALERT_SERVICE:-kit}
  local state=${ALERT_STATE_DIR:-/tmp/alerts} stamp payload now last
  echo "ALERT [$severity] $event: $message" >&2
  [ -n "${ALERT_WEBHOOK:-}" ] || return 0
  mkdir -p "$state"
  stamp="$state/$(printf '%s' "$event:$subject" | sha256sum | cut -c1-32)"
  now=$(date +%s)
  if [ -f "$stamp" ]; then
    last=$(cat "$stamp" 2>/dev/null || echo 0)
    [ $((now - last)) -ge "$ALERT_DEDUPE_S" ] || return 0
  fi
  case "$ALERT_WEBHOOK" in
    https://hooks.slack.com/*)
      payload=$(jq -cn --arg t "[$severity] $service: $message" '{text: $t}') ;;
    https://discord.com/api/webhooks/*|https://discordapp.com/api/webhooks/*)
      payload=$(jq -cn --arg t "[$severity] $service: $message" '{content: $t}') ;;
    *)
      payload=$(jq -cn --arg e "$event" --arg s "$severity" --arg svc "$service" --arg m "$message" \
        --arg subj "$subject" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{event: $e, severity: $s, service: $svc, message: $m, details: {subject: $subj}, ts: $ts}') ;;
  esac
  if curl -fsS --max-time 10 -H 'Content-Type: application/json' --data "$payload" "$ALERT_WEBHOOK" >/dev/null; then
    echo "$now" > "$stamp"
  else
    echo "alert delivery to the webhook failed" >&2
  fi
  return 0
}
