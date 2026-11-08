#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
. "$HERE/../../src/claude_kitchen/tc/lib/claim.sh"   # defines claim_slot / release_slot

SBX=$(mktemp -d); export TC_LOCK_DIR="$SBX/locks"; mkdir -p "$TC_LOCK_DIR"
LOCK="$TC_LOCK_DIR/tc-claim-plow"

# fresh claim wins and creates the lock dir
rc=0; claim_slot "$LOCK" || rc=$?
assert_rc 0 "$rc" "claim_slot: fresh claim wins"
assert_eq "yes" "$([ -d "$LOCK" ] && echo yes)" "claim_slot: lock dir exists after win"

# a second claim on a held (fresh) lock loses
rc=0; claim_slot "$LOCK" || rc=$?
assert_rc 1 "$rc" "claim_slot: held lock → lose"

# release frees it; a later claim wins again
release_slot "$LOCK"
assert_eq "" "$([ -d "$LOCK" ] && echo yes)" "release_slot: lock dir gone"
rc=0; claim_slot "$LOCK" || rc=$?
assert_rc 0 "$rc" "claim_slot: re-claim after release wins"

# a STALE lock (>4h) is stolen
release_slot "$LOCK"; mkdir "$LOCK"; touch -t 200001010000 "$LOCK"
rc=0; claim_slot "$LOCK" || rc=$?
assert_rc 0 "$rc" "claim_slot: steals a >4h-stale lock"

# a NON-EMPTY stale lock (e.g. leftover contents from a crashed run) is still
# reclaimable — removal is rm -rf via release_slot, not rmdir, so ENOTEMPTY can't
# silently wedge the slot. (mkdir the child AFTER touch would re-fresh mtime, so
# backdate last.)
release_slot "$LOCK"; mkdir "$LOCK" "$LOCK/.junk"; touch -t 200001010000 "$LOCK"
rc=0; claim_slot "$LOCK" || rc=$?
assert_rc 0 "$rc" "claim_slot: reclaims a non-empty stale lock (no ENOTEMPTY wedge)"

# Concurrency invariant: many racing reclaimers of ONE stale lock must yield
# exactly one winner — no double-claim. Exercises the remove-under-.steal-mutex then
# re-claim path under REAL concurrency (no faked primitives). The atomic mkdir is the
# sole winner-decider and a fresh lock is never removed, so exactly-one-winner holds.
release_slot "$LOCK"; mkdir "$LOCK"; touch -t 200001010000 "$LOCK"
WINS="$SBX/wins"; : > "$WINS"
for _i in 1 2 3 4 5 6 7 8; do ( claim_slot "$LOCK" && echo win >>"$WINS" ) & done
wait
assert_eq "1" "$(grep -c win "$WINS")" \
  "claim_slot: concurrent stale-steal yields exactly one winner"
# the `.steal` mutex must be cleaned up by the normal code paths — a leak would
# wedge every future reclaim of this slot.
assert_eq "" "$(ls "$TC_LOCK_DIR"/*.steal 2>/dev/null)" \
  "claim_slot: no .steal mutex leaked after the race"

# release_slot refuses an empty or non-claim path and does NOT rm it
DECOY="$SBX/decoy"; mkdir "$DECOY"
rc=0; release_slot "" || rc=$?
assert_rc 1 "$rc" "release_slot: refuses empty path"
rc=0; release_slot "$DECOY" || rc=$?
assert_rc 1 "$rc" "release_slot: refuses a non-claim path"
assert_eq "yes" "$([ -d "$DECOY" ] && echo yes)" "release_slot: non-claim path NOT deleted"

rm -rf "$SBX"
assert_summary
