#!/usr/bin/env bash
# git-sync: back the vault up to GIT_REMOTE (SPEC §11, plan D31/D32).
#
# Every SYNC_INTERVAL seconds, or right after agent-runner drops a commit request:
#   lock -> git add -A -> commit if dirty -> fetch -> rebase -> unlock -> push.
# On a rebase conflict the local version of each conflicting file is kept as
# <file>.conflict-<ts>.md, the rest is rebased and pushed, and an alert is sent.
# /data is a separate mount and never part of the repository.
set -euo pipefail

VAULT=${VAULT_DIR:-/vault}
DATA=${DATA_DIR:-/data}
STATE=$DATA/git-sync
REQUESTS=$STATE/requests
BRANCH=${GIT_BRANCH:-main}
INTERVAL=${SYNC_INTERVAL:-120}
LOCK=$DATA/locks/vault.lock
export ALERT_SERVICE=git-sync ALERT_STATE_DIR=$STATE/alerts
# shellcheck source=scripts/lib/alert.sh
source /usr/local/lib/kit/alert.sh

: "${GIT_REMOTE:?GIT_REMOTE is not set}"
mkdir -p "$STATE" "$REQUESTS" "$DATA/locks"
export GIT_CONFIG_GLOBAL=$STATE/gitconfig GIT_TERMINAL_PROMPT=0
git config --global user.name "docs-vault git-sync"
git config --global user.email "git-sync@docs-vault.invalid"
git config --global --replace-all safe.directory "$VAULT"
git config --global core.hooksPath /dev/null
git config --global init.defaultBranch "$BRANCH"
if [ -f "$STATE/ssh/id_ed25519" ]; then
  export GIT_SSH_COMMAND="ssh -i $STATE/ssh/id_ed25519 -o UserKnownHostsFile=$STATE/ssh/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes"
fi

log() { printf '%s git-sync: %s\n' "$(date -u +%FT%TZ)" "$*"; }
git_v() { git -C "$VAULT" "$@"; }

with_lock() {  # with_lock <command...>: run under the shared vault lock (D6)
  exec 9>"$LOCK"
  flock -w 120 9 || { log "could not get the vault lock in 120 s"; return 1; }
  echo git-sync > "$DATA/locks/vault.owner"
  local status=0
  "$@" || status=$?
  flock -u 9
  exec 9>&-
  return "$status"
}

remote_has_branch() { git_v ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; }

init_repo() {
  if [ -d "$VAULT/.git" ]; then
    git_v remote get-url origin >/dev/null 2>&1 || git_v remote add origin "$GIT_REMOTE"
    git_v remote set-url origin "$GIT_REMOTE"
    return
  fi
  log "initialising a repository in the vault"
  git_v init -q -b "$BRANCH"
  git_v remote add origin "$GIT_REMOTE"
  if remote_has_branch; then
    if [ "${GIT_ADOPT_REMOTE:-false}" != true ]; then
      send_alert git_sync_remote_not_empty high "GIT_REMOTE already has a '$BRANCH' branch; refusing to mix histories. Set GIT_ADOPT_REMOTE=true to rebase the vault onto it." remote
      rm -rf "$VAULT/.git"
      exit 1
    fi
    git_v fetch -q origin "$BRANCH"
  fi
}

setup_lfs() {
  [ "${GIT_LFS:-false}" = true ] || return 0
  git_v lfs install --local >/dev/null
  local pattern
  for pattern in 'raw/**/*.pdf' 'raw/**/*.png' 'raw/**/*.jpg' 'raw/**/*.zip' \
                 'attachments/**/*.pdf' 'attachments/**/*.png' 'attachments/**/*.jpg' 'attachments/**/*.zip'; do
    grep -qF "$pattern " "$VAULT/.gitattributes" 2>/dev/null || git_v lfs track "$pattern" >/dev/null
  done
}

commit_message() {  # the oldest commit request from agent-runner, or the default (D31)
  local request
  request=$(find "$REQUESTS" -maxdepth 1 -name '*.json' -type f | sort | head -n1)
  if [ -n "$request" ] && jq -e '.type and .summary' "$request" >/dev/null 2>&1; then
    printf 'docs(%s): %s\n\nAgent: agent-runner\n' \
      "$(jq -r '.type | gsub("[^a-z]"; "")' "$request")" "$(jq -r '.summary | gsub("\n"; " ") | .[0:200]' "$request")"
  else
    printf 'docs(auto): %s\n' "$(date -u +%FT%TZ)"
  fi
}

resolve_conflict() {
  local ts files f copy
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  mapfile -t files < <(git_v diff --name-only --diff-filter=U)
  git_v rebase --abort
  for f in "${files[@]}"; do
    [ -f "$VAULT/$f" ] || continue
    if [[ $f == *.md ]]; then copy="${f%.md}.conflict-$ts.md"; else copy="$f.conflict-$ts"; fi
    cp -p "$VAULT/$f" "$VAULT/$copy"
  done
  # During a rebase "ours" is the upstream: conflicting hunks take the remote version;
  # the local version survives in the .conflict copies.
  if ! git_v rebase -q -X ours "origin/$BRANCH"; then
    git_v rebase --abort 2>/dev/null || true
    touch "$STATE/blocked"
    send_alert git_sync_conflict high "git-sync could not rebase onto origin/$BRANCH (e.g. a file edited here was deleted there). Pushing is paused; resolve in $VAULT and delete $STATE/blocked." blocked
    return 1
  fi
  git_v add -A
  git_v commit -q -m "docs(auto): keep local versions of conflicting files ($ts)" || true
  send_alert git_sync_conflict high "Edits here and on the remote conflicted in: ${files[*]}. The remote version won; the local version is kept as *.conflict-$ts.md." "$ts"
}

sync_locked() {
  git_v add -A
  if ! git_v diff --cached --quiet; then
    git_v commit -q -F <(commit_message)
    log "committed: $(git_v log -1 --format=%s)"
  fi
  find "$REQUESTS" -maxdepth 1 -name '*.json' -type f -delete
  if remote_has_branch; then
    git_v fetch -q origin "$BRANCH"
    if git_v rev-parse -q --verify HEAD >/dev/null; then
      git_v rebase -q "origin/$BRANCH" 2>/dev/null || resolve_conflict
    else
      git_v reset -q --hard "origin/$BRANCH"
    fi
  fi
}

cycle() {
  if [ -f "$STATE/blocked" ]; then
    log "paused: delete $STATE/blocked after resolving the repository"
    return 0
  fi
  with_lock sync_locked || return 0
  git_v rev-parse -q --verify HEAD >/dev/null || return 0
  if git_v push -q origin "HEAD:$BRANCH" 2>"$STATE/push.err"; then
    rm -f "$STATE/push.err"
  else
    send_alert git_sync_push_failed warning "git push to GIT_REMOTE failed: $(tail -n 3 "$STATE/push.err" | tr '\n' ' ')" push
  fi
}

init_repo
setup_lfs
log "syncing $VAULT to $GIT_REMOTE ($BRANCH) every ${INTERVAL}s"
while true; do
  cycle
  touch "$STATE/heartbeat"
  # Wake on a commit request or after INTERVAL; beat every 30 s while waiting.
  waited=0
  while [ "$waited" -lt "$INTERVAL" ]; do
    chunk=$(( INTERVAL - waited < 30 ? INTERVAL - waited : 30 ))
    if inotifywait -qq -t "$chunk" -e close_write -e moved_to "$REQUESTS" 2>/dev/null; then
      break
    fi
    waited=$((waited + chunk))
    touch "$STATE/heartbeat"
  done
done
