#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
export KITCHEN_FLEET_HOSTS="$HERE/fixtures/fleet-hosts" KITCHEN_FLEET_REPOS="$HERE/fixtures/fleet-repos"
. "$HERE/../../src/claude_kitchen/tc/lib/pick.sh"    # defines _tcp <repo-dir> <tag> <host>
. "$HERE/../../src/claude_kitchen/tc/lib/claim.sh"   # defines claim_slot — used to pin the real lock contract

# Sandbox: fake HOME + fake git/tmux on PATH driven by env vars.
SBX=$(mktemp -d); export HOME="$SBX"
mkdir -p "$SBX/Hacking/plow0/.git" "$SBX/Hacking/plow1/.git" "$SBX/Hacking/plow2/.git" "$SBX/bin"
mkdir -p "$SBX/locks"; export TC_LOCK_DIR="$SBX/locks"

# Fake git: branch from GIT_BRANCH_<base>, porcelain status from GIT_DIRTY_<base>.
cat >"$SBX/bin/git" <<'EOF'
#!/usr/bin/env bash
dir=""; [ "$1" = "-C" ] && { dir=$2; shift 2; }
base=$(basename "$dir")
case "$1 $2" in
  "symbolic-ref --short") eval "echo \${GIT_BRANCH_${base}:-main}";;
  "status --porcelain")   eval "printf '%s' \"\${GIT_DIRTY_${base}:-}\"";;
  *) exit 0;;
esac
EOF
# Fake tmux servers: each file in the socket dir is one server, listing its panes
# (path|command). `panes <server> <lines>` sets one; `tmux -S <file>` reads it.
export TMUX_TMPDIR="$SBX/sock"; SOCKS="$TMUX_TMPDIR/tmux-$(id -u)"; mkdir -p "$SOCKS"
panes() { printf '%b' "$2" >"$SOCKS/$1"; }
cat >"$SBX/bin/tmux" <<'EOF'
#!/usr/bin/env bash
[ "$1 $3 $4" = "-S list-panes -a" ] && cat "$2"
EOF
chmod +x "$SBX/bin/git" "$SBX/bin/tmux"
export PATH="$SBX/bin:$PATH"

# all clean on main, nothing busy → picks slot 0
out=$(_tcp plow plow hosta)
assert_eq "plow0-hosta $SBX/Hacking/plow0" "$out" "picks clean slot 0"

# slot 0 dirty → picks plow1
out=$(export GIT_DIRTY_plow0="M f"; _tcp plow plow hosta)
assert_eq "plow1-hosta $SBX/Hacking/plow1" "$out" "skips dirty slot 0"

# slot 0 on feature branch + plow1 busy (claude) → lands plow2
panes default "$SBX/Hacking/plow1|claude\n"
out=$(export GIT_BRANCH_plow0=feat/x; _tcp plow plow hosta)
assert_eq "plow2-hosta $SBX/Hacking/plow2" "$out" "skips not-on-main and busy"

# pane in plow1 but just a shell → plow1 counts as free
panes default "$SBX/Hacking/plow1|zsh\n"
out=$(export GIT_BRANCH_plow0=feat/x; _tcp plow plow hosta)
assert_eq "plow1-hosta $SBX/Hacking/plow1" "$out" "idle shell pane is not busy"

# a kitchen cook on its own `-L ck-*` server makes the slot busy too
panes ck-boss "$SBX/Hacking/plow1|claude\n"
out=$(export GIT_BRANCH_plow0=feat/x; _tcp plow plow hosta)
assert_eq "plow2-hosta $SBX/Hacking/plow2" "$out" "a pane on another tmux server is busy"
# ...even when a later server's query fails under tc's pipefail (a dir makes the fake's cat fail)
mkdir "$SOCKS/zz-stale"
out=$(set -o pipefail; export GIT_BRANCH_plow0=feat/x; _tcp plow plow hosta)
assert_eq "plow2-hosta $SBX/Hacking/plow2" "$out" "a failing last server does not hide a busy pane"
rm -rf "$SOCKS"/*

# a kitchen cook's record holds its slot with no pane up (a suspended kitchen), until
# clock-out deletes the record; a cwd that only shares a prefix (plow1 vs plow10) doesn't
mkdir -p "$SBX/.claude-kitchen/boss/cooks"
printf '{\n  "agent": "eng",\n  "cwd": "%s"\n}\n' "$SBX/Hacking/plow0" >"$SBX/.claude-kitchen/boss/cooks/eng.json"
printf '{\n  "cwd": "%s"\n}\n' "$SBX/Hacking/plow10" >"$SBX/.claude-kitchen/boss/cooks/ios.json"
out=$(_tcp plow plow hosta)
assert_eq "plow1-hosta $SBX/Hacking/plow1" "$out" "a kitchen cook's recorded cwd holds the slot"
rm "$SBX/.claude-kitchen/boss/cooks/eng.json"
out=$(_tcp plow plow hosta)
assert_eq "plow0-hosta $SBX/Hacking/plow0" "$out" "clock-out (record gone) frees the slot"
rm -rf "$SBX/.claude-kitchen"

# tag arg drives the session name independent of the repo dir
out=$(_tcp plow xyz hosta)
assert_eq "xyz0-hosta $SBX/Hacking/plow0" "$out" "tag arg drives session name"

# none free → rc 1
rc=0
( export GIT_BRANCH_plow0=feat/a GIT_BRANCH_plow1=feat/b GIT_BRANCH_plow2=feat/c
  _tcp plow plow hosta ) >/dev/null 2>&1 || rc=$?
assert_rc 1 "$rc" "no free workspace → rc 1"

# a fresh lock made by the REAL claim_slot primitive hides the slot → lands plow1
# (pins the cross-lib contract: a lock claim_slot creates is one _tcp honors —
# not just pick's own naming asserted against itself).
claim_slot "$SBX/locks/tc-claim-plow0"
out=$(_tcp plow plow hosta)
assert_eq "plow1-hosta $SBX/Hacking/plow1" "$out" "skips a fresh-lock-held slot"

# a STALE (>4h) lock does NOT hide the slot → slot 0 is picked again
touch -t 200001010000 "$SBX/locks/tc-claim-plow0"
out=$(_tcp plow plow hosta)
assert_eq "plow0-hosta $SBX/Hacking/plow0" "$out" "stale lock (>4h) does not hide the slot"
rmdir "$SBX/locks/tc-claim-plow0"

# a fresh FILE-typed lock (stray /tmp debris) also hides the slot — pins the lock
# half of the `-e` check (a `-d` there would offer the slot and spin claim_slot).
: > "$SBX/locks/tc-claim-plow0"
out=$(_tcp plow plow hosta)
assert_eq "plow1-hosta $SBX/Hacking/plow1" "$out" "skips a fresh file-typed lock"
rm -f "$SBX/locks/tc-claim-plow0"

# a `.steal` (reclaim in progress, or leaked) hides the slot → lands plow1, so tc
# claim can't spin on a lock claim_slot can't reclaim. Use a FILE here (not a dir):
# stray /tmp debris is equally unreclaimable, and it pins the `-e` (not `-d`) check.
: > "$SBX/locks/tc-claim-plow0.steal"
out=$(_tcp plow plow hosta)
assert_eq "plow1-hosta $SBX/Hacking/plow1" "$out" "skips a slot with a .steal (file-typed too)"
rm -f "$SBX/locks/tc-claim-plow0.steal"

rm -rf "$SBX"
assert_summary
