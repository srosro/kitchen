# claim.sh — shared atomic slot-claim primitive for the tc workspace fleet.
# Sourced LOCALLY only — by `tc claim` and by the /cleanup skill (both run on the
# same host as the caller; never shipped inline over ssh, unlike lib/pick.sh).
#
# Lock namespace — `claim_lock_path` below is the single source of truth; every
# LOCAL caller (tc claim, /cleanup) builds the path through it. lib/pick.sh is the
# ONE unavoidable hand-mirror — it ships inline over ssh and can't source this — so
# it documents the mirror at its lock-check; the smoke fence guards the shared
# staleness window between the two.
#     ${TC_LOCK_DIR:-/tmp}/tc-claim-<basename-of-slot-dir>
# TC_LOCK_DIR defaults to /tmp in production; tests point it at a sandbox so they
# never collide with a real claim in /tmp.
#
# A claim is an atomic `mkdir` (POSIX-atomic) — the ONLY winner-decider, so two
# callers can never both win the same path. A lock older than 4h is reclaimed on the
# load-bearing assumption that its holder is DEAD (a live claimer holds a lock for
# milliseconds — `tc claim` releases right after `checkout -b`; cleanup, for one
# pass): a reclaimer REMOVES the stale lock (under a short-lived `<lock>.steal` mutex
# so racing reclaimers don't both delete-and-recreate) and re-claims via the same
# `mkdir`. Under that dead-holder assumption a fresh lock is never stale, so it's
# never removed → no double-claim. The removal briefly vacates the path, but that
# window is benign: claiming is only ever the atomic `mkdir`, with no put-it-back
# step, so whoever wins the next mkdir is the sole holder. (Replaces the former
# mv-aside-into-a-grave steal, whose RESTORE step let a third racer double-claim —
# see PR #93 probe 1.)
#
# Accepted crash window: a reclaimer killed while holding `<lock>.steal` leaks that
# mutex. Recover with `rmdir <lock>.steal`; the slot then self-heals on the next
# claim (the stale lock gets removed-and-reclaimed normally). Not auto-healed — an
# auto-rmdir of an "old" `.steal` could race a live one.

claim_lock_path() { # claim_lock_path <slot-dir> — the lock path for a workspace slot
    printf '%s\n' "${TC_LOCK_DIR:-/tmp}/tc-claim-$(basename "$1")"
}

claim_slot() { # claim_slot <lock-path> — atomic claim; 0 = won, 1 = lost/held
    if mkdir "$1" 2>/dev/null; then
        return 0
    fi
    # Stale leftover? Remove it under the steal mutex (re-checking staleness under the
    # mutex so a fresher replacement isn't removed), then fall through to re-claim.
    # release_slot (guarded rm -rf) — not rmdir — so a non-empty stale lock can't
    # wedge the slot by failing ENOTEMPTY.
    if [ -n "$(find "$1" -maxdepth 0 -mmin +240 2>/dev/null)" ] && mkdir "$1.steal" 2>/dev/null; then
        [ -n "$(find "$1" -maxdepth 0 -mmin +240 2>/dev/null)" ] && release_slot "$1"
        rmdir "$1.steal"
    fi
    mkdir "$1" 2>/dev/null && return 0
    return 1
}

release_slot() { # release_slot <lock-path> — rm the claim lock; refuses a non-claim path
    case ${1:-} in
        */tc-claim-*) rm -rf "$1" ;;
        *) echo "release_slot: refusing to rm non-claim path '${1:-}'" >&2; return 1 ;;
    esac
}
