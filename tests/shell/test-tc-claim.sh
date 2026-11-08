#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
TC="$HERE/../../src/claude_kitchen/tc/tc"
export KITCHEN_FLEET_REPOS="$HERE/fixtures/fleet-repos"
# No real tmux servers: the busy check scans every socket in this dir.
export TMUX_TMPDIR=/nonexistent-tc-claim-test

# Build a sandbox HOME with real git repos on main (tag `cc` → repo `code-config`).
mk_sbx() { # mk_sbx <slot> <slot> ... ; echoes the sandbox dir
  local sbx; sbx=$(mktemp -d)
  mkdir -p "$sbx/Hacking" "$sbx/locks"
  local d
  for d in "$@"; do
    git init -q -b main "$sbx/Hacking/$d"
    git -C "$sbx/Hacking/$d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  done
  printf '%s\n' "$sbx"
}

# 1. claim returns a slot dir now on the new branch, and releases the lock
SBX=$(mk_sbx code-config0 code-config2 code-config3)
dir=$(HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/foo 2>/dev/null)
# exact equality, not contains — stdout must be JUST the dir (no git chatter leak)
assert_eq "$SBX/Hacking/code-config0" "$dir" "claim: returns exactly the code-config0 slot dir"
assert_eq "feat/foo" "$(git -C "$dir" symbolic-ref --short HEAD)" "claim: slot is on the new branch"
assert_eq "" "$(ls "$SBX/locks")" "claim: lock released after success"
rm -rf "$SBX"

# 2. parallel race: two concurrent claims must get two DISTINCT slots
SBX=$(mk_sbx code-config0 code-config2 code-config3)
HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/a >"$SBX/a" 2>/dev/null &
HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/b >"$SBX/b" 2>/dev/null &
wait
da=$(cat "$SBX/a"); db=$(cat "$SBX/b")
assert_eq "1" "$([ -n "$da" ] && [ -n "$db" ] && [ "$da" != "$db" ] && echo 1)" \
  "claim: two concurrent claims return distinct slots"
rm -rf "$SBX"

# 3. exhaustion: every slot on a feature branch → rc 1, stderr message, no checkout made
SBX=$(mk_sbx code-config0 code-config2)
for d in code-config0 code-config2; do git -C "$SBX/Hacking/$d" checkout -q -b feat/busy; done
rc=0; err=$(HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/x 2>&1 >/dev/null) || rc=$?
assert_rc 1 "$rc" "claim: exhaustion → rc 1"
assert_contains "$err" "no free" "claim: exhaustion → 'no free' on stderr"
rm -rf "$SBX"

# 4. a live lock on the first slot is skipped → second slot is claimed
SBX=$(mk_sbx code-config0 code-config2)
mkdir "$SBX/locks/tc-claim-code-config0"   # pre-claim slot 0 (fresh lock)
dir=$(HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/y 2>/dev/null)
assert_eq "$SBX/Hacking/code-config2" "$dir" "claim: skips a lock-held slot, picks the next"
rm -rf "$SBX"

# 5. arg validation: bad branch (metachar) and wrong arg count fail rc 2
HOME=/tmp "$TC" claim cc 'feat/x;rm' >/dev/null 2>&1; assert_rc 2 $? "claim: rejects metachar branch"
HOME=/tmp "$TC" claim cc >/dev/null 2>&1; assert_rc 2 $? "claim: wrong arg count → usage rc 2"

# 6. branch already exists in the picked slot → checkout -b fails → release + rc 1,
# and the failure must NOT burn other free slots (fail loud, don't silently re-pick).
SBX=$(mk_sbx code-config0 code-config2)
git -C "$SBX/Hacking/code-config0" branch feat/dup    # pre-create the target on slot 0 (still on main)
rc=0; HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/dup >/dev/null 2>&1 || rc=$?
assert_rc 1 "$rc" "claim: pre-existing branch in slot → rc 1"
assert_eq "" "$(ls "$SBX/locks")" "claim: lock released after checkout-b failure"
assert_eq "main" "$(git -C "$SBX/Hacking/code-config2" symbolic-ref --short HEAD)" \
  "claim: branch-exists failure does not burn other slots"
rm -rf "$SBX"

# 7. errexit safety: a slot that reads as main for _tcp but whose in-lock re-check
# symbolic-ref FAILS (slot detached / removed in the TOCTOU window) must release the
# lock and re-pick — NOT abort tc claim under set -e and leak the just-won lock.
# Fake git: symbolic-ref returns main on its 1st call per slot (the _tcp check), then
# exits 1 (the in-lock re-check); clean status; no real repo needed.
SBX=$(mktemp -d); mkdir -p "$SBX/Hacking/code-config0/.git" "$SBX/Hacking/code-config2/.git" "$SBX/locks" "$SBX/bin"
cat >"$SBX/bin/git" <<'FAKE'
#!/usr/bin/env bash
d=""; [ "$1" = "-C" ] && { d=$2; shift 2; }
case "$1 $2" in
  "symbolic-ref --short")
    c="$d/.symref-n"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n+1)) >"$c"
    [ "$n" = 0 ] && echo main || exit 1 ;;   # main once (for _tcp), then fail (re-check)
  "status --porcelain") : ;;                 # clean
  *) : ;;
esac
FAKE
chmod +x "$SBX/bin/git"
rc=0; PATH="$SBX/bin:$PATH" HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/q >/dev/null 2>&1 || rc=$?
assert_rc 1 "$rc" "claim: in-lock symbolic-ref failure exhausts cleanly → rc 1 (no set -e abort)"
assert_eq "" "$(ls "$SBX/locks")" "claim: failed re-check releases the lock (no leak under set -e)"
rm -rf "$SBX"

# 8. an UNRECLAIMABLE stale lock (stale + a leaked .steal mutex) must make tc claim
# fail loud, not spin forever: _tcp skips a slot with a .steal, so the sole slot
# drops out → "no free workspace" → rc 1. perl alarm caps the run so a regression
# (e.g. _tcp losing the .steal-skip) surfaces as a timeout, not a hang.
SBX=$(mk_sbx code-config0)
mkdir "$SBX/locks/tc-claim-code-config0" "$SBX/locks/tc-claim-code-config0.steal"
touch -t 200001010000 "$SBX/locks/tc-claim-code-config0"   # stale (>4h) → _tcp offers it
rc=0; HOME="$SBX" TC_LOCK_DIR="$SBX/locks" perl -e 'alarm 10; exec @ARGV' "$TC" claim cc feat/q >/dev/null 2>&1 || rc=$?
assert_rc 1 "$rc" "claim: unreclaimable stale lock → fails loud (rc 1, no infinite spin)"
rm -rf "$SBX"

# 9. in-lock branch re-check: a slot that reads `main` for _tcp but turns to a
# feature branch by the in-lock re-check (a concurrent claim branched it) must be
# released and skipped, landing on the next slot. Fake git: code-config0 reads
# `main` on its 1st symbolic-ref (the _tcp check) then `feat/race` after (the
# re-check + subsequent _tcp), code-config2 stays `main`.
SBX=$(mktemp -d); mkdir -p "$SBX/Hacking/code-config0/.git" "$SBX/Hacking/code-config2/.git" "$SBX/locks" "$SBX/bin"
cat >"$SBX/bin/git" <<'FAKE'
#!/usr/bin/env bash
d=""; [ "$1" = "-C" ] && { d=$2; shift 2; }
case "$1 $2" in
  "symbolic-ref --short")
    c="$d/.symref-n"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n+1)) >"$c"
    if [ "$(basename "$d")" = code-config0 ] && [ "$n" != 0 ]; then echo feat/race; else echo main; fi ;;
  "status --porcelain") : ;;   # clean
  *) : ;;                      # checkout -b etc. succeed silently
esac
FAKE
chmod +x "$SBX/bin/git"
# perl alarm caps the run: an inverted re-check (release-and-continue on main too)
# would spin _tcp forever, and the cap surfaces that as a failed assert, not a hang.
dir=$(PATH="$SBX/bin:$PATH" HOME="$SBX" TC_LOCK_DIR="$SBX/locks" perl -e 'alarm 10; exec @ARGV' "$TC" claim cc feat/q 2>/dev/null)
assert_eq "$SBX/Hacking/code-config2" "$dir" "claim: re-check sees a branched slot → releases + lands the next slot"
assert_eq "" "$(ls "$SBX/locks")" "claim: branch-changed re-check releases the lock"
rm -rf "$SBX"

# 10. in-lock CLEAN re-check: a slot clean for _tcp but dirtied by the in-lock
# re-check (a user/`tc new` touched it in the window) must be released and skipped,
# not branched over. Fake git: code-config0's status is clean on call 1 (the _tcp
# check) then dirty after (the re-check + later _tcp); code-config2 stays clean.
SBX=$(mktemp -d); mkdir -p "$SBX/Hacking/code-config0/.git" "$SBX/Hacking/code-config2/.git" "$SBX/locks" "$SBX/bin"
cat >"$SBX/bin/git" <<'FAKE'
#!/usr/bin/env bash
d=""; [ "$1" = "-C" ] && { d=$2; shift 2; }
case "$1 $2" in
  "symbolic-ref --short") echo main ;;
  "status --porcelain")
    c="$d/.status-n"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n+1)) >"$c"
    [ "$(basename "$d")" = code-config0 ] && [ "$n" != 0 ] && echo " M f" ;;   # dirty after call 1
  *) : ;;
esac
FAKE
chmod +x "$SBX/bin/git"
dir=$(PATH="$SBX/bin:$PATH" HOME="$SBX" TC_LOCK_DIR="$SBX/locks" perl -e 'alarm 10; exec @ARGV' "$TC" claim cc feat/q 2>/dev/null)
assert_eq "$SBX/Hacking/code-config2" "$dir" "claim: re-check sees a dirtied slot → releases + lands the next slot"
assert_eq "" "$(ls "$SBX/locks")" "claim: dirtied-slot re-check releases the lock"
rm -rf "$SBX"

# 11. in-lock BUSY re-check: a slot idle for _tcp but busy-in-tmux by the in-lock
# re-check (a `tc new`/human opened a non-shell pane in the window) must be released
# and skipped. Fake git: always main+clean. Fake tmux: reports code-config0's pane
# idle on call 1 (the _tcp selection check) then busy (`claude`) thereafter.
SBX=$(mktemp -d); mkdir -p "$SBX/Hacking/code-config0/.git" "$SBX/Hacking/code-config2/.git" "$SBX/locks" "$SBX/bin"
cat >"$SBX/bin/git" <<'FAKE'
#!/usr/bin/env bash
d=""; [ "$1" = "-C" ] && { d=$2; shift 2; }
case "$1 $2" in
  "symbolic-ref --short") echo main ;;
  "status --porcelain") : ;;   # always clean
  *) : ;;
esac
FAKE
cat >"$SBX/bin/tmux" <<'FAKE'
#!/usr/bin/env bash
[ "$1 $3 $4" = "-S list-panes -a" ] || exit 0
c="$HOME/.tmux-n"; n=$(cat "$c" 2>/dev/null || echo 0); echo $((n+1)) >"$c"
# code-config0's pane runs `claude` (busy) on every call after the 1st (_tcp's check)
[ "$n" != 0 ] && printf '%s/Hacking/code-config0|claude\n' "$HOME"
FAKE
chmod +x "$SBX/bin/git" "$SBX/bin/tmux"
dir=$(PATH="$SBX/bin:$PATH" HOME="$SBX" TC_LOCK_DIR="$SBX/locks" perl -e 'alarm 10; exec @ARGV' "$TC" claim cc feat/q 2>/dev/null)
assert_eq "$SBX/Hacking/code-config2" "$dir" "claim: re-check sees a busy slot → releases + lands the next slot"
assert_eq "" "$(ls "$SBX/locks")" "claim: busy-slot re-check releases the lock"
rm -rf "$SBX"

# 12. exact-slot claim: claim only the requested slot under its lock. A held or
# ineligible target must fail instead of silently selecting another free slot.
SBX=$(mk_sbx code-config0 code-config2)
dir=$(HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/exact 2 2>/dev/null)
assert_eq "$SBX/Hacking/code-config2" "$dir" "claim exact: returns the requested slot"
assert_eq "feat/exact" "$(git -C "$dir" symbolic-ref --short HEAD)" "claim exact: branches the requested slot"

git -C "$SBX/Hacking/code-config2" checkout -q main
mkdir "$SBX/locks/tc-claim-code-config2"
rc=0; HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/held 2 >/dev/null 2>&1 || rc=$?
assert_rc 1 "$rc" "claim exact: held target fails"
assert_eq "main" "$(git -C "$SBX/Hacking/code-config0" symbolic-ref --short HEAD)" \
  "claim exact: held target does not fall back"
rmdir "$SBX/locks/tc-claim-code-config2"

git -C "$SBX/Hacking/code-config2" checkout -q -b feat/busy
rc=0; HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" claim cc feat/ineligible 2 >/dev/null 2>&1 || rc=$?
assert_rc 1 "$rc" "claim exact: ineligible target fails"
assert_eq "main" "$(git -C "$SBX/Hacking/code-config0" symbolic-ref --short HEAD)" \
  "claim exact: ineligible target does not fall back"
rm -rf "$SBX"

assert_summary
