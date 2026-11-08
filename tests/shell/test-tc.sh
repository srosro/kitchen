#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
export KITCHEN_FLEET_HOSTS="$HERE/fixtures/fleet-hosts" KITCHEN_FLEET_REPOS="$HERE/fixtures/fleet-repos"
TC="$HERE/../../src/claude_kitchen/tc/tc"

# slot connect: tc <repo> <N> <host>
out=$(TC_DRYRUN=1 "$TC" plow 0 hosta)
assert_contains "$out" "host=hosta"        "slot 0: host"
assert_contains "$out" "-s plow0-hosta"    "slot 0: session name"
assert_contains "$out" "d=~/Hacking/plow0;" "slot 0: dir assign"
assert_contains "$out" "tmux new -A"        "slot 0: tmux attach-or-create"

out=$(TC_DRYRUN=1 "$TC" plow 4 hostb)
assert_contains "$out" "host=hostb"           "slot 4: host"
assert_contains "$out" "-s plow4-hostb"       "slot 4: session"
assert_contains "$out" "d=~/Hacking/plow4;" "slot 4: dir"

# tag resolves to canonical session + full dir
out=$(TC_DRYRUN=1 "$TC" kwr 2 hosta)
assert_contains "$out" "-s kwr2-hosta"                      "tag: session"
assert_contains "$out" "d=~/Hacking/knightwatch-reviewer2;" "tag: dir"

# host alias canonicalizes: both spellings must yield ONE session name, else the
# same slot forks two tmux sessions depending on how the host was typed.
out=$(TC_DRYRUN=1 "$TC" plow 5 hc)
assert_contains "$out" "host=hostc-laptop"     "alias: canonical ssh target"
assert_contains "$out" "-s plow5-hostc-laptop" "alias: canonical session name"
assert_contains "$out" "d=~/Hacking/plow5;"        "alias: dir"

# the other two entry points canonicalize too — `setup` is the one that clones ten
# checkouts onto the target, so an un-canonicalized alias there misses self-loopback.
assert_contains "$(TC_DRYRUN=1 "$TC" setup plow hc)" "setup-host=hostc-laptop" "alias: setup canonicalizes"
out=$(TC_DRYRUN=1 "$TC" new plow hc)
assert_contains "$out" "host=hostc-laptop"                  "alias: new canonicalizes ssh target"
# _tcp builds the session name from this arg, so the raw alias reaching it is the
# same forked-session regression the slot-connect block above pins.
assert_contains "$out" 'out=$(_tcp plow plow hostc-laptop)' "alias: new passes canonical host to the picker"

# unknown host fails
TC_DRYRUN=1 "$TC" plow 0 nosuchhost >/dev/null 2>&1; assert_rc 1 $? "unknown host rc"

# bad usage fails rc 2
TC_DRYRUN=1 "$TC" >/dev/null 2>&1; assert_rc 2 $? "no args usage rc"
TC_DRYRUN=1 "$TC" plow hosta >/dev/null 2>&1; assert_rc 2 $? "bare two-arg form removed rc"
TC_DRYRUN=1 "$TC" plow x hosta >/dev/null 2>&1; assert_rc 2 $? "non-numeric slot rc"
TC_DRYRUN=1 "$TC" plow 10 hosta >/dev/null 2>&1; assert_rc 2 $? "out-of-range slot rc"

# safe-name guard: metacharacter repo args are rejected (rc 2) before any command builds
TC_DRYRUN=1 "$TC" 'plow;touch x' 0 hosta >/dev/null 2>&1; assert_rc 2 $? "connect: rejects metachar repo"
TC_DRYRUN=1 "$TC" setup 'kwr;touch x' hosta >/dev/null 2>&1; assert_rc 2 $? "setup: rejects metachar repo"

# setup: credential-bearing URL refused (rc 2); plain remotes allowed
TC_DRYRUN=1 "$TC" setup plow hosta 'https://tok@github.com/x.git' >/dev/null 2>&1; assert_rc 2 $? "setup: rejects credential URL"
out=$(TC_DRYRUN=1 "$TC" setup plow hosta 'https://github.com/example/plow.git')
assert_contains "$out" "setup-url-given=yes" "setup: plain https allowed"

# setup: too many args fails usage (rc 2) instead of silently dropping the extra
TC_DRYRUN=1 "$TC" setup plow hosta url extra >/dev/null 2>&1; assert_rc 2 $? "setup: rejects extra arg"

# setup: dry-run shows host + repo; no url given
out=$(TC_DRYRUN=1 "$TC" setup plow hosta)
assert_contains "$out" "setup-host=hosta"  "setup: host"
assert_contains "$out" "setup-repo=plow"    "setup: repo"
assert_contains "$out" "setup-url-given=no" "setup: no url"

# setup: run the shipped provision snippet (dry-run output from its `set -e`
# opener) in a sandbox HOME with a fake git — verifies the legacy bare-origin
# fallback, the 0..9 clone loop, idempotent re-run skipping, and that a slot
# whose origin differs only by a trailing .git or / (cosmetic, same remote) is
# skipped on re-run rather than aborting with "different origin". The fake git
# returns the .git-suffixed form for *2 and the trailing-slash form for *3.
SBX=$(mktemp -d)
mkdir -p "$SBX/Hacking/plow/.git" "$SBX/bin"
cat >"$SBX/bin/git" <<'EOF'
#!/usr/bin/env bash
dir=.
[ "$1" = -C ] && { dir=$2; shift 2; }
case $1 in
  remote) case $dir in
            *2) echo "git@example.com:fake/plow.git" ;;   # cosmetic .git suffix
            *3) echo "git@example.com:fake/plow/" ;;       # cosmetic trailing slash
            *4) echo "https://example.com/fake/plow.git" ;; # same remote over HTTPS
            *)  echo "git@example.com:fake/plow" ;;
          esac ;;
  clone)  echo "$2 $3" >>"$GITLOG"; mkdir -p "$3/.git" ;;
esac
EOF
chmod +x "$SBX/bin/git"
snippet=$(TC_DRYRUN=1 "$TC" setup plow hosta | sed -n '/^set -e$/,$p')
printf '\n' | HOME="$SBX" PATH="$SBX/bin:$PATH" GITLOG="$SBX/clones" TC_REPO=plow bash -c "$snippet" >/dev/null
assert_eq 10 "$(wc -l <"$SBX/clones" | tr -d ' ')" "setup run: clones 10 slots"
assert_contains "$(cat "$SBX/clones")" "git@example.com:fake/plow plow0" "setup run: slot 0 from legacy bare origin"
assert_contains "$(cat "$SBX/clones")" "git@example.com:fake/plow plow9" "setup run: slot 9 cloned"
out=$(printf '\n' | HOME="$SBX" PATH="$SBX/bin:$PATH" GITLOG="$SBX/clones" TC_REPO=plow bash -c "$snippet" 2>&1); rc=$?
assert_rc 0 "$rc"                                "setup re-run: cosmetic origin drift is not a mismatch"
assert_eq 10 "$(wc -l <"$SBX/clones" | tr -d ' ')" "setup re-run: idempotent, no new clones"
assert_contains "$out" "skip:  plow2"            "setup re-run: normalized origin skips .git-suffixed slot"
assert_contains "$out" "skip:  plow3"            "setup re-run: normalized origin skips trailing-slash slot"
assert_contains "$out" "skip:  plow4"            "setup re-run: normalized origin skips HTTPS-cloned slot"
assert_not_contains "$out" "different origin"    "setup re-run: no false origin-mismatch on cosmetic drift"
rm -rf "$SBX"

# new: ships the picker (_tcp) inline, runs it on the host, splits "<sess> <dir>"
out=$(TC_DRYRUN=1 "$TC" new plow hosta)
assert_contains "$out" "host=hosta"                  "new: host"
assert_contains "$out" '_tcp()'                       "new: ships picker body inline"
assert_contains "$out" 'out=$(_tcp plow plow hosta)' "new: invokes _tcp with repo tag host"
assert_contains "$out" 'tmux new -A -s "${out%% *}"'  "new: splits session"
assert_contains "$out" 'c "${out#* }"'                "new: splits dir"

# setup: tag resolves to repo dir name; raw url is redacted from diagnostics
url="git@github.com:example/knightwatch-reviewer.git"
out=$(TC_DRYRUN=1 "$TC" setup kwr hostb "$url")
assert_contains "$out"     "setup-repo=knightwatch-reviewer" "setup: tag→repo"
assert_contains "$out"     "setup-url-given=yes"             "setup: url given"
assert_not_contains "$out" "$url"                            "setup: raw url not echoed"

assert_summary
