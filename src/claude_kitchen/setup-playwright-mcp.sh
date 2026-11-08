#!/usr/bin/env bash
# Provision the Playwright MCP server that powers the `playwright` skill.
# Registers it per-machine in user scope, picking browser flags by platform,
# and ensures a usable Chromium. Idempotent — `kitchen setup` calls it every
# run; it fast-paths to a no-op once set up. Self-skips when the claude CLI
# isn't installed.
set -euo pipefail

# Single pinned version. The bundled-Chromium download (Linux without a system
# browser) derives its Playwright version from this, so there's one thing to
# bump. Bump deliberately and re-verify the skill's tool names when you do.
MCP_VERSION=0.0.75

# Pure + testable (smoke-install.sh sources this and asserts the mapping):
# given OS and a resolved Chromium path (empty if none), echo the args that
# follow `npx @playwright/mcp@<v>` in the registration. macOS runs headed so you
# can watch; headless Linux runs --headless and points --executable-path at the
# resolved browser — a system Chromium when present (the only option on ARM,
# where Playwright ships no bundled build), else the bundled build main()
# downloads. main() resolves that binary, then calls this with it. NOTE:
# --no-sandbox is deliberately NOT here — main() adds it per-host only when the
# sandboxed launch test fails, so the sandbox stays on wherever it works.
derive_mcp_args() {
  local os="$1" chromium="${2:-}"
  local args="--isolated"
  if [[ "$os" == "Linux" ]]; then
    args="$args --headless"
    [[ -n "$chromium" ]] && args="$args --executable-path $chromium"
  fi
  echo "$args"
}

# Locate claude — login shells on some boxes (Ubuntu/Debian) miss ~/.local/bin.
find_claude() {
  command -v claude 2>/dev/null && return 0
  [[ -x "$HOME/.local/bin/claude" ]] && { echo "$HOME/.local/bin/claude"; return 0; }
  return 1
}

# Headless launch smoke for a chromium binary, with any extra flags.
launch_ok() {
  local bin="$1"; shift
  timeout 30 "$bin" --headless=new --disable-gpu --dump-dom about:blank "$@" \
    >/dev/null 2>/tmp/kitchen-cr-launch.err
}

main() {

  local claude
  claude="$(find_claude)" || {
    echo "setup-playwright-mcp: claude CLI not found — skipping (install Claude Code, then re-run 'kitchen setup')"
    return 0
  }

  local os chromium test_bin
  os="$(uname -s)"
  chromium=""
  test_bin=""
  [[ "$os" == "Linux" ]] && chromium="$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null || true)"

  # Decide --no-sandbox per host BEFORE the fast-path, by launch-testing the
  # actual browser: keep the sandbox on (page content is untrusted), fall back
  # to --no-sandbox only where the sandboxed launch fails (e.g. a kernel with
  # unprivileged user namespaces disabled). Computing it here — not after a
  # prefix-matched fast-path — means a stale --no-sandbox registration is kept
  # only on a host that still needs it, not blindly. On Linux without a system
  # Chromium, download Playwright's matching build first (no sudo, ~/.cache);
  # skip the download when it's already cached so the common path stays cheap.
  local sandbox_flag="" pw_ver=""
  if [[ "$os" == "Linux" ]]; then
    if [[ -n "$chromium" ]]; then
      test_bin="$chromium"
    else
      # Already registered at this pin with a bundled build that still exists:
      # reuse it, so a re-run needs no network. Otherwise resolve it below.
      test_bin="$("$claude" mcp list 2>/dev/null | sed -n "s|^playwright: npx @playwright/mcp@$MCP_VERSION .*--executable-path \([^ ]*\).*|\1|p" | head -1)"
      [[ -x "$test_bin" ]] || test_bin=""
    fi
    if [[ "$os" == "Linux" && -z "$test_bin" ]]; then
      # Bundled: let Playwright own the cache. `install` is idempotent — it
      # no-ops when the build matching the pinned MCP version is already present,
      # and pulls the right one when only an older build is cached. Resolve the
      # version from @playwright/mcp's own dependency so MCP_VERSION stays the
      # only pin, and refuse a non-exact version so `npx` can't run a floating
      # installer.
      pw_ver="$(npm view "@playwright/mcp@$MCP_VERSION" dependencies.playwright 2>/dev/null || true)"
      [[ "$pw_ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] \
        || { echo "setup-playwright-mcp: refusing non-exact Playwright version '$pw_ver' for @playwright/mcp@$MCP_VERSION" >&2; return 1; }
      echo "setup-playwright-mcp: ensuring Playwright Chromium ($pw_ver) ..."
      npx --yes "playwright@$pw_ver" install chromium
      # Launch-test the *exact* pinned build, not a cache-wide first match: a
      # stale cached build could otherwise decide --no-sandbox from a browser
      # the server won't run. --dry-run reports the pinned build's location;
      # the server launches this same build via its own pinned playwright-core.
      local loc
      loc="$(npx --yes "playwright@$pw_ver" install chromium --dry-run 2>/dev/null | awk '/Install location:/{print $NF; exit}')"
      test_bin="$(find "$loc" -type f -name chrome 2>/dev/null | head -1 || true)"
    fi
    if [[ -n "$test_bin" ]]; then
      if launch_ok "$test_bin"; then
        echo "setup-playwright-mcp: ✓ browser launches headless, sandboxed"
      elif launch_ok "$test_bin" --no-sandbox; then
        sandbox_flag="--no-sandbox"
        echo "setup-playwright-mcp: sandboxed launch failed on this host — using --no-sandbox" >&2
      else
        echo "setup-playwright-mcp: WARNING — $test_bin failed to launch headless:" >&2
        tail -2 /tmp/kitchen-cr-launch.err >&2 2>/dev/null || true
        [[ -z "$chromium" ]] && echo "    fix (run once on this host): sudo npx playwright@${pw_ver:-<version>} install-deps chromium" >&2
      fi
    fi
  fi

  # Register against the SAME binary we launch-tested — a system Chromium when
  # present, else the bundled build resolved above. Deriving base_args here (not
  # from the system-only $chromium) is what wires --executable-path on a headless
  # Linux host that has no system browser; without it the MCP falls back to the
  # absent `chrome` channel and every navigation fails.
  local base_args
  base_args="$(derive_mcp_args "$os" "$test_bin")"
  local args="$base_args"
  [[ -n "$sandbox_flag" ]] && args="$base_args $sandbox_flag"

  # Codex reads the same skill (kitchen links it into ~/.agents/skills), so give it
  # the same server. Re-registering is local and cheap, so no fast path here.
  local codex
  codex="$(command -v codex 2>/dev/null || true)"
  [[ -z "$codex" && -x "$HOME/.local/bin/codex" ]] && codex="$HOME/.local/bin/codex"
  if [[ -n "$codex" ]]; then
    "$codex" mcp remove playwright >/dev/null 2>&1 || true
    "$codex" mcp add playwright -- npx "@playwright/mcp@$MCP_VERSION" $args >/dev/null
    echo "setup-playwright-mcp: registered with Codex: npx @playwright/mcp@$MCP_VERSION $args"
  fi

  # Fast-path: already registered with exactly this command. The trailing ` - `
  # (the `claude mcp list` "<cmd> - <status>" separator) anchors an exact match,
  # so `--headless` doesn't spuriously match a `--headless --no-sandbox` line.
  if "$claude" mcp list 2>/dev/null | grep -qF "playwright: npx @playwright/mcp@$MCP_VERSION $args - "; then
    echo "setup-playwright-mcp: already registered (npx @playwright/mcp@$MCP_VERSION $args) — nothing to do"
    return 0
  fi

  # (Re-)register idempotently — remove clears any drifted prior entry.
  "$claude" mcp remove -s user playwright >/dev/null 2>&1 || true
  # args is a deliberately word-split flag list.
  # shellcheck disable=SC2086
  "$claude" mcp add -s user playwright -- npx "@playwright/mcp@$MCP_VERSION" $args
  echo "setup-playwright-mcp: registered: npx @playwright/mcp@$MCP_VERSION $args"

  if "$claude" mcp list 2>/dev/null | grep -qE "playwright: .* ✓ Connected"; then
    echo "setup-playwright-mcp: ✓ Connected"
  else
    echo "setup-playwright-mcp: WARNING — registered but not Connected; run 'claude mcp list' to inspect" >&2
  fi
}

# Run main only when executed directly; sourcing (the smoke test) just defines
# the functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
