#!/usr/bin/env bash
set -u
HERE=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/assert.sh"
export KITCHEN_FLEET_HOSTS="$HERE/fixtures/fleet-hosts" KITCHEN_FLEET_REPOS="$HERE/fixtures/fleet-repos"
. "$HERE/../../src/claude_kitchen/tc/lib/fleet.sh"

# fleet_repo: tag → repo, repo → repo, unknown → passthrough
assert_eq "knightwatch-reviewer" "$(fleet_repo kwr)"                  "repo from tag"
assert_eq "knightwatch-reviewer" "$(fleet_repo knightwatch-reviewer)" "repo from dir name"
assert_eq "newthing"             "$(fleet_repo newthing)"            "repo unknown passthrough"

# fleet_tag: tag → tag, repo → tag, unknown → dir name
assert_eq "kwr"      "$(fleet_tag kwr)"                  "tag from tag"
assert_eq "kwr"      "$(fleet_tag knightwatch-reviewer)" "tag from dir name"
assert_eq "cc"       "$(fleet_tag code-config)"          "tag cc"
assert_eq "newthing" "$(fleet_tag newthing)"             "tag unknown → dir name"

# no fleet-repos file: every repo is named by its dir
assert_eq "my-app" "$(KITCHEN_FLEET_REPOS=/nonexistent fleet_tag my-app)" "no repo table → dir name"

# fleet_user
assert_eq "alice"      "$(fleet_user hosta)" "user hosta"
assert_eq "dave"       "$(fleet_user hostf)" "user hostf"
assert_eq "bob"        "$(fleet_user hoste)"  "user hoste"
assert_eq "bob"        "$(fleet_user hostc-laptop)" "user hostc-laptop"
assert_eq "carol"      "$(fleet_user hostd)"  "user hostd"
_self=$(hostname -s 2>/dev/null || hostname)
_err=$(KITCHEN_FLEET_HOSTS=/nonexistent fleet_user hosta 2>&1 >/dev/null); _rc=$?
assert_rc 1 $_rc "missing host table fails"
case $_err in *"/nonexistent"*) _named=yes;; *) _named=no;; esac
assert_eq "yes" "$_named" "missing host table error names the path"

fleet_is_self "$_self"; assert_rc 0 $? "fleet_is_self matches this host"
fleet_is_self "$(printf '%s' "$_self" | tr '[:lower:]' '[:upper:]')"; assert_rc 0 $? "fleet_is_self case-folds"
fleet_is_self "not-this-host"; assert_rc 1 $? "fleet_is_self rejects a different host"
# An alias must read as self on the box it names, or a consumer passing it ssh's
# to this machine and gets a publickey denial. The map is faked so the assertion
# doesn't depend on which host the suite runs on, in a subshell so the override
# is contained — but the asserts run in the parent, whose counters they mutate.
# Rendered once rather than kept as a format string: a variable used AS printf's
# format would eat a literal % in a future row.
_fake_map=$(printf '%s so selfalias\notherbox so otheralias\n' "$_self")
( _fleet_hosts() { printf '%s\n' "$_fake_map"; }
  fleet_is_self selfalias ); assert_rc 0 $? "fleet_is_self resolves an alias to this host"
# ...without over-matching: another host's alias resolves and is still not self.
# The rc-1 case alone would also pass if the override never took effect, so pin
# that resolution actually happened.
assert_eq "otherbox" "$( _fleet_hosts() { printf '%s\n' "$_fake_map"; }; fleet_host otheralias )" \
  "fake map is live: otheralias → otherbox"
( _fleet_hosts() { printf '%s\n' "$_fake_map"; }
  fleet_is_self otheralias ); assert_rc 1 $? "fleet_is_self: another host's alias is not self"
fleet_is_self "$_self.local"; assert_rc 0 $? "fleet_is_self matches the macOS .local form"
fleet_user bogushost  >/dev/null 2>&1; assert_rc 1 $? "unknown host errors"

# fleet_host: canonical in → canonical out, alias in → canonical out.
assert_eq "hostc-laptop" "$(fleet_host hostc-laptop)" "host: canonical passthrough"
assert_eq "hostc-laptop" "$(fleet_host hc)"           "host: alias hc → hostc-laptop"
assert_eq "bob"          "$(fleet_user hc)"           "user: resolves via alias"
assert_eq "hosta"        "$(fleet_host hosta)"        "host: aliasless row passthrough"
fleet_host bogushost >/dev/null 2>&1; assert_rc 1 $? "fleet_host unknown errors"

# an unknown host names the alternatives — the bare error left the caller guessing
_err=$(fleet_host bogushost 2>&1 >/dev/null)
assert_contains "$_err" "hostc-laptop"  "unknown host: lists known hosts"
assert_contains "$_err" "(alias: hc)"   "unknown host: lists aliases"

# fleet_session: <tag><N>-<host>
assert_eq "app0-hosta"  "$(fleet_session my-app 0 hosta)"               "session slot 0"
assert_eq "app4-hostb"  "$(fleet_session app 4 hostb)"                  "session slot 4"
assert_eq "kwr3-hosta"  "$(fleet_session knightwatch-reviewer 3 hosta)" "session from dir name"

# fleet_dir: ~/Hacking/<repo><N> (tilde left literal for remote expansion)
assert_eq '~/Hacking/my-app0'               "$(fleet_dir app 0)" "dir slot 0"
assert_eq '~/Hacking/my-app4'               "$(fleet_dir my-app 4)" "dir slot 4"
assert_eq '~/Hacking/knightwatch-reviewer2' "$(fleet_dir kwr 2)"  "dir from tag"

assert_summary
