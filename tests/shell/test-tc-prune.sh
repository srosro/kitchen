#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
TC="$HERE/../../src/claude_kitchen/tc/tc"
export KITCHEN_FLEET_REPOS="$HERE/fixtures/fleet-repos"
export TMUX_TMPDIR=/nonexistent-tc-prune-test

# Sandbox HOME with rs0..rs5 and rs10, each a git repo (a Cargo workspace) holding a
# target/, every file aged past the 24h window unless a case freshens it.
SBX=$(mktemp -d); mkdir -p "$SBX/Hacking" "$SBX/locks" "$SBX/.cargo/bin"
printf '#!/bin/sh\n' >"$SBX/.cargo/bin/sccache"; chmod +x "$SBX/.cargo/bin/sccache"
for n in 0 1 2 3 4 5 10; do
  d=$SBX/Hacking/rs$n
  git init -q -b main "$d"; mkdir -p "$d/target/debug/deps"; echo x >"$d/target/debug/deps/a"
  touch "$d/Cargo.toml"
  find "$d" -exec touch -h -t 202001010000 {} +
done
chmod a-w "$SBX/Hacking/rs0/target/debug/deps"                          # rs0: a file it can't delete
touch "$SBX/Hacking/rs1/target/debug"                                  # rs1: built just now
mkdir -p "$SBX/Hacking/rs4/src"; echo fn >"$SBX/Hacking/rs4/src/main.rs"   # rs4: source edited just now
touch -t 202001010000 "$SBX/Hacking/rs4/src" "$SBX/Hacking/rs4"
mkdir "$SBX/locks/tc-claim-rs2"                                         # rs2: claimed
mkdir -p "$SBX/.claude-kitchen/k/cooks"                                 # rs3: a cook works there
printf '{"cwd": "%s"}\n' "$SBX/Hacking/rs3" >"$SBX/.claude-kitchen/k/cooks/c.json"
mkdir -p "$SBX/Hacking/rs4/.cargo"; echo own >"$SBX/Hacking/rs4/.cargo/config.toml"   # rs4: repo's own cargo config

run() { HOME="$SBX" TC_LOCK_DIR="$SBX/locks" "$TC" prune-targets rs; }

out=$(TC_DRYRUN=1 run)
assert_contains "$out" "would prune" "prune: dry run lists the idle slot"
assert_eq "7" "$(ls -d "$SBX"/Hacking/rs*/target | wc -l | tr -d ' ')" "prune: dry run deletes nothing"
assert_contains "$out" "would sccache $SBX/Hacking/rs5" "prune: dry run lists the sccache config"
assert_eq "" "$(ls "$SBX"/Hacking/rs5/.cargo 2>/dev/null)" "prune: dry run writes no config"

out=$(run)
assert_eq "" "$(ls -d "$SBX"/Hacking/rs5/target 2>/dev/null)" "prune: an idle slot loses target/"
assert_contains "$out" "incomplete $SBX/Hacking/rs0/target" "prune: an undeletable file is reported, not fatal"
assert_contains "$out" "skip $SBX/Hacking/rs1" "prune: carries on to the next slot after a failed delete"
assert_contains "$out" "skip $SBX/Hacking/rs4 (touched within 24h)" "prune: a nested source edit keeps target/"
assert_contains "$out" "skip $SBX/Hacking/rs1 (touched within 24h)" "prune: a fresh build is kept"
assert_contains "$out" "skip $SBX/Hacking/rs2 (claimed)" "prune: a claimed slot is kept"
assert_contains "$out" "skip $SBX/Hacking/rs3 (busy)" "prune: a slot a cook works in is kept"
assert_eq "" "$(ls -d "$SBX"/Hacking/rs10/target 2>/dev/null)" "prune: a two-digit slot is pruned too"
assert_contains "$(cat "$SBX/Hacking/rs5/.cargo/config.toml")" "rustc-wrapper = \"$SBX/.cargo/bin/sccache\"" "sccache: an idle slot builds through sccache"
assert_eq "" "$(git -C "$SBX/Hacking/rs5" status --porcelain -- .cargo)" "sccache: the config leaves git status clean"
assert_contains "$(cat "$SBX/Hacking/rs1/.cargo/config.toml")" "rustc-wrapper" "sccache: a freshly built slot gets it too"
assert_eq "" "$(ls "$SBX"/Hacking/rs3/.cargo 2>/dev/null)" "sccache: a busy slot is left alone"
assert_eq "own" "$(cat "$SBX/Hacking/rs4/.cargo/config.toml")" "sccache: a repo's own cargo config is left alone"
assert_eq "tc-claim-rs2" "$(ls "$SBX/locks")" "prune: releases its own locks, leaves the live claim"
chmod -R u+w "$SBX"; rm -rf "$SBX"
assert_summary
