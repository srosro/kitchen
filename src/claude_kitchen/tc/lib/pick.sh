# pick.sh — next-free-workspace resolver for `tc new`. NOT a standalone CLI:
# sourced by tc (self-loopback) and tests, and `cat`-shipped inline over ssh for
# remote hosts — so pick.sh needs no remote *install*, but `tmux` must resolve on
# the remote's non-login PATH (see README; git/awk/find are in /usr/bin on both
# platforms, so a bare PATH still finds those). Defines _tcp.
# Free = on main/master + clean working tree + no live claim lock + not busy (no
# tmux pane whose cwd is the dir runs a non-shell foreground command). Prints
# "<session> <dir>" for the first free candidate among <repo>0..<repo>9;
# returns 1 if none qualify.
# POSIX-shell safe (no `local`, no bashisms) since the remote login shell is
# unknown.

# 0 if a tmux pane whose cwd is <dir> runs a non-shell foreground command, on any
# of this user's tmux servers — kitchen runs its cooks on `-L ck-*` servers — or
# if a kitchen cook's record names <dir> as its cwd: the record lives until
# clock-out, so it holds the slot while the cook's window is down (a suspended kitchen).
_tcp_busy() { # _tcp_busy <dir>
  # A subshell: zsh (a remote login shell) fails an unmatched glob, which would
  # otherwise abort this function before the tmux check below.
  (grep -qsF "\"cwd\": \"$1\"" "$HOME"/.claude-kitchen/*/cooks/*.json) 2>/dev/null && return 0
  (for _tcp_s in "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"/*; do   # subshell: same zsh no-match containment
    tmux -S "$_tcp_s" list-panes -a -F '#{pane_current_path}|#{pane_current_command}' 2>/dev/null || :
  done) 2>/dev/null | awk -F'|' -v d="$1" '$1==d && $2 !~ /^-?(bash|zsh|sh|fish)$/ {f=1} END{exit !f}'
}

# 0 if <dir> is on main/master + clean working tree + not busy — the git/tmux half
# of "free". Shared by _tcp (selection) AND `tc claim`'s in-lock re-check, so the two
# can't drift (the re-check forgetting a predicate is exactly what PR #93's clean-
# workspace bug was). The lock/.steal checks stay _tcp-only (selection, not re-check).
_tcp_eligible() { # _tcp_eligible <dir>
  _tcp_br=$(git -C "$1" symbolic-ref --short HEAD 2>/dev/null) || return 1
  [ "$_tcp_br" = main ] || [ "$_tcp_br" = master ] || return 1
  [ -z "$(git -C "$1" status --porcelain 2>/dev/null)" ] || return 1
  ! _tcp_busy "$1"
}

_tcp() { # _tcp <repo-dir> <tag> <host>
  _tcp_repo=$1 _tcp_tag=$2 _tcp_host=$3
  for _tcp_n in 0 1 2 3 4 5 6 7 8 9; do
    _tcp_d=$HOME/Hacking/$_tcp_repo$_tcp_n
    [ -d "$_tcp_d/.git" ] || continue
    _tcp_eligible "$_tcp_d" || continue
    # Skip a slot with a LIVE claim lock (the real cross-subagent/-fork interlock;
    # see lib/claim.sh). Same 4h staleness window as claim.sh so a crashed claimer's
    # lock can't hide a slot forever. This path is the lone inline MIRROR of
    # claim.sh's claim_lock_path (pick ships inline over ssh, can't source it) —
    # keep the `tc-claim-` prefix in sync. Basename of the dir == repo+n.
    # -e not -d at both paths: claim_slot makes dirs, but stray /tmp debris (a FILE
    # at the lock or .steal path) is equally unreclaimable (mkdir fails EEXIST), so
    # offering the slot would spin tc claim — skip on existence of any type.
    _tcp_lock=${TC_LOCK_DIR:-/tmp}/tc-claim-$_tcp_repo$_tcp_n
    [ -e "$_tcp_lock" ] && [ -z "$(find "$_tcp_lock" -maxdepth 0 -mmin +240 2>/dev/null)" ] && continue
    # Also skip a slot mid-reclaim or with a leaked `.steal` mutex (see claim_slot).
    [ -e "$_tcp_lock.steal" ] && continue
    printf '%s %s\n' "$_tcp_tag$_tcp_n-$_tcp_host" "$_tcp_d"
    return 0
  done
  echo "tc new: no free $_tcp_repo workspace on $_tcp_host" >&2
  return 1
}
