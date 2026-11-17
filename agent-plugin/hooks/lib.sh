# shellcheck shell=bash
# Shared by the docs-vault session hooks (SPEC-session-hooks) and scripts/agent-plugin.
# Sourced, never executed. Bash 3.2 (macOS) compatible; callers set LC_ALL=C.
# Nothing here reads credentials, uses the network or evaluates repository data as code.

# Opt-out for the whole session: DOCS_VAULT_CAPTURE=off (also 0, false, no).
dv_capture_on() {
  case $(printf '%s' "${DOCS_VAULT_CAPTURE:-on}" | tr '[:upper:]' '[:lower:]') in
    off | 0 | false | no) return 1 ;;
  esac
  return 0
}

# Prints the host/path identity of a git remote, the same as vault-core's
# vault_core.repos.normalise_remote: SSH, HTTPS and bare forms agree; URL credentials are stripped,
# non-empty queries/fragments and local paths are refused. GitHub paths are lower-cased
# (owner/name only), other hosts keep path case; a non-default port remains in the identity.
dv_normalise_remote() {
  local value=$1 scheme rest netloc remainder hostinfo host port path seg
  local scp_re='^([^/@:[:space:]]+@)?(\[[^]]+]|[^/:[:space:]]+):(.+)$'
  local scheme_re='^[A-Za-z][A-Za-z0-9+.-]*$' control_re='%(0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])'
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  case $value in '' | *[[:space:][:cntrl:]]* | *\\*) return 1 ;; esac
  case $value in /* | .* | \~*) return 1 ;; esac
  if [[ $value != *://* ]]; then
    if [[ $value =~ $scp_re ]] && { seg=${BASH_REMATCH[3]%%/*}; [ -z "$seg" ] || [[ $seg == *[!0-9]* ]]; }; then
      path=${BASH_REMATCH[3]}
      value="ssh://${BASH_REMATCH[2]}/${path#"${path%%[!/]*}"}"
    else
      host=${value%%/*}
      host=${host%%:*}
      [[ $value == */* ]] || return 1
      [[ $host == *.* || $host == localhost || $host == \[* ]] || return 1
      value="https://$value"
    fi
  fi

  scheme=${value%%://*}
  [[ $scheme =~ $scheme_re ]] || return 1
  scheme=$(printf '%s' "$scheme" | tr '[:upper:]' '[:lower:]')
  rest=${value#*://}
  netloc=${rest%%[/?#]*}
  remainder=${rest#"$netloc"}
  if [[ $remainder == *#* ]]; then
    [ -z "${remainder#*#}" ] || return 1
    remainder=${remainder%%#*}
  fi
  if [[ $remainder == *\?* ]]; then
    [ -z "${remainder#*\?}" ] || return 1
    remainder=${remainder%%\?*}
  fi
  path=$remainder

  [[ $netloc != *@* || ${netloc%@*} != *[][]* ]] || return 1
  hostinfo=${netloc##*@}
  [[ $hostinfo == \[* || $hostinfo != *[][]* ]] || return 1
  if [[ $hostinfo == \[* ]]; then
    host=${hostinfo#\[}
    [[ $host == *\]* ]] || return 1
    port=${host#*\]}
    host=${host%%\]*}
    [[ -n $host && $host != *[!0-9A-Fa-f:.]* ]] || return 1
    port=${port#*:}
    [[ $hostinfo == *\]:* ]] || port=""
  else
    host=${hostinfo%%:*}
    port=""
    [[ $hostinfo != *:* ]] || port=${hostinfo#*:}
  fi
  [ -n "$host" ] || return 1
  host=$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')
  if [ -n "$port" ]; then
    [[ $port != *[!0-9]* ]] || return 1
    port=${port#"${port%%[!0]*}"}
    port=${port:-0}
    [[ ${#port} -le 5 && $port -le 65535 ]] || return 1
  fi
  case $scheme in
    https) [ "$port" != 443 ] || port="" ;;
    http) [ "$port" != 80 ] || port="" ;;
    ssh) [ "$port" != 22 ] || port="" ;;
    git) [ "$port" != 9418 ] || port="" ;;
    *) return 1 ;;
  esac
  [[ $host != *:* ]] || host="[$host]"
  [ -z "$port" ] || host="$host:$port"

  if [[ $path == *%* ]]; then
    # Escapes that decode to control characters are refused, like any other control character.
    [[ ! $path =~ $control_re ]] || return 1
    path=$(printf '%b' "$(printf '%s' "$path" | sed 's/%\([0-9A-Fa-f][0-9A-Fa-f]\)/\\x\1/g')") || return 1
  fi
  path=${path#"${path%%[!/]*}"}
  path=${path%"${path##*[!/]}"}
  case $(printf '%s' "$path" | tr '[:upper:]' '[:lower:]') in *.git) path=${path%????} ;; esac
  [ -n "$path" ] || return 1
  case "/$path/" in *//* | */./* | */../*) return 1 ;; esac
  case $path in *[[:space:][:cntrl:]]* | *\\*) return 1 ;; esac
  if [ "$host" = github.com ]; then
    [[ $path == */* && ${path#*/} != */* ]] || return 1
    path=$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')
  fi
  printf '%s\n' "$host/$path"
}

# A capture-allowlist entry: host[:port]/path exactly, or host[:port]/path/* for any repo directly
# under that path. Host lower-case; github.com entries are lower-case owner/name or owner/*.
dv_valid_pattern() {
  local pattern=$1 path
  local re='^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[A-Za-z0-9._-]+)+(/\*)?$'
  [[ $pattern =~ $re ]] || return 1
  case "$pattern/" in */./* | */../*) return 1 ;; esac
  if [[ $pattern == github.com/* ]]; then
    path=${pattern#github.com/}
    [[ $path == */* && ${path#*/} != */* && $path != *[A-Z]* ]] || return 1
  fi
  return 0
}

# Is the repo identity in the allowlist file (one entry per line; invalid lines are ignored)?
dv_allowed() {
  local repo=$1 list=$2 pattern prefix rest
  while IFS= read -r pattern || [ -n "$pattern" ]; do
    dv_valid_pattern "$pattern" || continue
    if [[ $pattern == */\* ]]; then
      prefix=${pattern%\*}
      [[ $repo == "$prefix"* ]] || continue
      rest=${repo#"$prefix"}
      [[ -n $rest && $rest != */* ]] && return 0
    else
      [ "$repo" = "$pattern" ] && return 0
    fi
  done <"$list"
  return 1
}

# Read-only git with no optional locks, prompts or fsmonitor; stderr dropped.
dv_git() {
  local dir=$1
  shift
  GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0 git -c core.fsmonitor=false -C "$dir" "$@" 2>/dev/null
}

# Prints the identity as JSON when capture applies to the repo containing $1: no opt-out,
# and an origin in allowlist $2. Fails otherwise.
dv_resolve() {
  local cwd=$1 list=$2 root remote repo branch
  local safe_re='^[][A-Za-z0-9._~:/-]+$'
  dv_capture_on || return 1
  [[ -f $list && -s $list ]] || return 1
  command -v git >/dev/null 2>&1 || return 1
  [[ -n $cwd && -d $cwd ]] || return 1
  root=$(dv_git "$cwd" rev-parse --show-toplevel) || return 1
  [[ -n $root && -d $root ]] || return 1
  [[ ! -e $root/.docs-vault-ignore && ! -L $root/.docs-vault-ignore ]] || return 1
  remote=$(dv_git "$root" remote get-url origin) || return 1
  repo=$(dv_normalise_remote "$remote") || return 1
  [[ ${#repo} -le 200 && $repo =~ $safe_re ]] || return 1
  dv_allowed "$repo" "$list" || return 1
  branch=$(dv_git "$root" symbolic-ref --short -q HEAD) || branch=""
  [[ ${#branch} -le 100 && $branch != *[!A-Za-z0-9._/-]* ]] || branch=""
  jq -cn --arg repo "$repo" --arg branch "$branch" \
    '{repo: $repo} + (if $branch == "" then {} else {branch: $branch} end)'
}

# String field $2 of the JSON object $1, empty when absent or not a string; fails on bad JSON.
dv_field() {
  jq -r --arg key "$2" '.[$key] | if type == "string" then . else "" end' <<<"$1"
}

dv_hash() {
  local digest
  if command -v sha256sum >/dev/null 2>&1; then
    digest=$(printf '%s' "$1" | sha256sum) || return 1
  elif command -v shasum >/dev/null 2>&1; then
    digest=$(printf '%s' "$1" | shasum -a 256) || return 1
  else
    return 1
  fi
  digest=${digest%% *}
  [[ ${#digest} -eq 64 && $digest != *[!0-9a-f]* ]] || return 1
  printf '%s\n' "$digest"
}

# Counts prompts per session and prints this one's number. State is one counter per session in
# a private folder ($TMPDIR/docs-vault-hooks-<uid>, mode 0700, files 0600) named by a hash of the
# session id, so the id never becomes a path. Refuses a folder that is not ours or not private.
dv_next_count() {
  local sid=$1 dir key file count="" tmp
  [[ -n $sid && ${#sid} -le 256 ]] || return 1
  dir=${TMPDIR:-/tmp}
  dir="${dir%/}/docs-vault-hooks-$(id -u)" || return 1
  if [ ! -e "$dir" ] && [ ! -L "$dir" ]; then
    mkdir -m 700 "$dir" 2>/dev/null || return 1
  fi
  [[ -d $dir && ! -L $dir && -O $dir ]] || return 1
  case $(ls -ld "$dir") in drwx------*) ;; *) return 1 ;; esac
  key=$(dv_hash "docs-vault-session:$sid") || return 1
  file="$dir/$key"
  if [ -e "$file" ] || [ -L "$file" ]; then
    [[ -f $file && ! -L $file && -O $file ]] || return 1
    count=$(head -c 12 "$file") || count=""
  fi
  [[ -n $count && ${#count} -le 9 && $count != *[!0-9]* ]] || count=0
  count=$((10#$count + 1))
  tmp="$dir/.tmp.$key.$$"
  if ! (umask 077 && printf '%s\n' "$count" >"$tmp") || ! mv -f "$tmp" "$file"; then
    rm -f "$tmp"
    return 1
  fi
  printf '%s\n' "$count"
}
