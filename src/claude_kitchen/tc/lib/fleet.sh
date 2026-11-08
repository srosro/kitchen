#!/usr/bin/env bash
# fleet.sh — fleet maps + pure resolvers for `tc`. Sourced, never executed.
# bash-3.2 safe: no associative arrays. All fns print to stdout, return non-zero on error.

# Repo tags: "<tag> <repo-dir>" rows in the operator's fleet-repos file (optional;
# without it every repo is named by its dir). Short tags name tmux sessions.
_fleet_pairs() {
  local f=${KITCHEN_FLEET_REPOS:-$HOME/.config/kitchen/fleet-repos}
  [[ -r "$f" ]] || return 0
  grep -v -E '^[[:space:]]*(#|$)' "$f"
}
# Canonical host→SSH-user map (data file: <host> <user> [alias] per line), plus an optional short alias in column 3 (the
# operator's skills delegate usernames here). Host aliases and repo tags are
# separate namespaces resolved from different arg positions, so one short name may
# be both a repo tag and a host alias.
# Adding an alias: rows match in order and the first hit wins, so a column-3 alias
# must not equal any column-1 hostname or any other row's column-3 alias — either
# silently reroutes that host (or leaves the alias dead on arrival). Two or more
# aliases warrant a table-wide test; one doesn't.
_fleet_hosts() {
  local f=${KITCHEN_FLEET_HOSTS:-$HOME/.config/kitchen/fleet-hosts}
  [[ -r "$f" ]] || { echo "fleet: host table not found at $f (one '<host> <user> [alias]' row per line)" >&2; return 1; }
  grep -v -E '^[[:space:]]*(#|$)' "$f"
}

fleet_repo() { # <tag-or-repo> -> repo dir (passthrough if unknown)
  local x=$1 tag repo
  while read -r tag repo; do
    if [ "$x" = "$tag" ] || [ "$x" = "$repo" ]; then printf '%s\n' "$repo"; return; fi
  done < <(_fleet_pairs)
  printf '%s\n' "$x"
}

fleet_tag() { # <tag-or-repo> -> tag (dir name if unknown)
  local x=$1 tag repo
  while read -r tag repo; do
    if [ "$x" = "$tag" ] || [ "$x" = "$repo" ]; then printf '%s\n' "$tag"; return; fi
  done < <(_fleet_pairs)
  printf '%s\n' "$x"
}

# <host-or-alias> -> "<canonical-host> <ssh-user>" (rc 1 if unknown). Both halves
# come from one scan so the alias match can't drift between name and user. On a
# miss, list the known hosts — the bare name alone left the caller guessing.
_fleet_resolve() {
  local x=$1 h u a
  while read -r h u a; do
    if [ "$x" = "$h" ] || { [ -n "$a" ] && [ "$x" = "$a" ]; }; then
      printf '%s %s\n' "$h" "$u"; return
    fi
  done < <(_fleet_hosts)
  { echo "tc: unknown host '$x'. Known hosts:"
    _fleet_hosts | awk '{ printf "  %s%s\n", $1, ($3 == "" ? "" : "  (alias: " $3 ")") }'
  } >&2
  return 1
}

# Canonicalize BEFORE naming a tmux session or testing self-loopback: both compare
# the host string as typed, so an unresolved alias would fork `app5-hc` from
# `app5-hostc-laptop` (two sessions, one dir) and defeat fleet_is_self.
fleet_host() { local r; r=$(_fleet_resolve "$1") || return 1; printf '%s\n' "${r%% *}"; }
fleet_user() { local r; r=$(_fleet_resolve "$1") || return 1; printf '%s\n' "${r#* }"; }

# True when <host> IS this machine (self-loopback). Tailnet names are lowercase
# but a macOS LocalHostName is not (`Hosts-MacBook-Pro`), so compare case-folded —
# a case-sensitive miss falls through to ssh-to-self and a publickey denial.
# Also strips a trailing `.local`, so the macOS FQDN form matches too.
fleet_is_self() { # <host-or-alias>
  local self lc_self lc_host h
  # Canonicalize first, so every fleet_* resolver takes an alias or a hostname
  # interchangeably. Without this, an alias reads as NOT-self on the very box it
  # names and the caller ssh's to itself (publickey denial) — the failure the
  # self-loopback rule exists to prevent. Unknown names pass through to the
  # comparison below, which is what callers testing an arbitrary name expect.
  h=$(fleet_host "$1" 2>/dev/null) || h=$1
  self=$(hostname -s 2>/dev/null || hostname)
  lc_self=$(printf '%s' "${self%.local}" | tr '[:upper:]' '[:lower:]')
  lc_host=$(printf '%s' "${h%.local}" | tr '[:upper:]' '[:lower:]')
  [ "$lc_self" = "$lc_host" ]
}

fleet_session() { printf '%s%s-%s\n' "$(fleet_tag "$1")" "$2" "$3"; } # <repo> <N|''> <host>
fleet_dir()     { printf '~/Hacking/%s%s\n' "$(fleet_repo "$1")" "$2"; } # <repo> <N|''>
