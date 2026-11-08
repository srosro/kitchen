"""setup-playwright-mcp.sh: the per-platform flag mapping, and main()'s wiring of the
resolved browser into `claude mcp add` (driven with faked externals, no download)."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "src" / "claude_kitchen" / "setup-playwright-mcp.sh"


def bash(code: str, **env) -> str:
    return subprocess.run(["bash", "-c", f"source {SCRIPT}\n{code}"], capture_output=True, text=True,
                          check=True, env={"PATH": "/usr/bin:/bin", **env}).stdout.strip()


@pytest.mark.parametrize("os_name, chromium, expected", [
    ("Darwin", "", "--isolated"),  # macOS registers headed so you can watch
    ("Linux", "/usr/bin/chromium", "--isolated --headless --executable-path /usr/bin/chromium"),
    ("Linux", "/x/ms-playwright/chrome-linux64/chrome", "--isolated --headless --executable-path /x/ms-playwright/chrome-linux64/chrome"),
    ("Linux", "", "--isolated --headless"),  # no resolvable browser
])
def test_flags_by_platform(os_name, chromium, expected):
    assert bash(f'derive_mcp_args {os_name} "{chromium}"') == expected


def test_main_registers_the_bundled_chromium_on_headless_linux(tmp_path):
    """A headless Linux host with no system browser must register --executable-path
    pointing at the bundled Chromium, not fall back to an absent `chrome` channel."""
    (tmp_path / "bin").mkdir()
    chrome = tmp_path / "pw" / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    claude = tmp_path / "bin" / "claude"
    claude.write_text('#!/usr/bin/env bash\necho "$*" >> "$ARGV_LOG"\n')  # logs argv; `mcp list` prints nothing
    chrome.write_text("#!/usr/bin/env bash\nexit 0\n")
    claude.chmod(0o755), chrome.chmod(0o755)
    log = tmp_path / "argv.log"
    bash(f"""
find_claude() {{ echo {claude}; }}
uname() {{ echo Linux; }}
command() {{ if [[ "$1" == -v ]]; then case "$2" in chromium|chromium-browser|google-chrome|google-chrome-stable|chrome|chrome-browser) return 1;; esac; fi; builtin command "$@"; }}
npm() {{ case "$*" in view*dependencies.playwright) echo 1.61.0;; *) echo "unexpected npm: $*" >&2; return 1;; esac; }}
npx() {{ case "$*" in *--dry-run*) echo "Install location: {tmp_path}/pw";; *"install chromium"*) : ;; *) echo "unexpected npx: $*" >&2; return 1;; esac; }}
main >/dev/null 2>&1
echo "$MCP_VERSION"
""", HOME=str(tmp_path), ARGV_LOG=str(log))
    version = bash("echo $MCP_VERSION")
    assert f"mcp add -s user playwright -- npx @playwright/mcp@{version} --isolated --headless --executable-path {chrome}" in log.read_text()


def test_rerun_when_registered_needs_no_network(tmp_path):
    """Already registered at the pin with a bundled Chromium that still exists: a
    re-run reuses that path and calls neither npm nor npx (an offline re-run passes)."""
    version = bash("echo $MCP_VERSION")
    (tmp_path / "bin").mkdir()
    chrome = tmp_path / "pw" / "chrome"
    chrome.parent.mkdir()
    chrome.write_text("#!/usr/bin/env bash\nexit 0\n")
    chrome.chmod(0o755)
    registered = f"playwright: npx @playwright/mcp@{version} --isolated --headless --executable-path {chrome} - ✓ Connected"
    claude = tmp_path / "bin" / "claude"
    claude.write_text(f'#!/usr/bin/env bash\necho "$*" >> "$ARGV_LOG"\n[[ "$1 $2" == "mcp list" ]] && echo "{registered}"\nexit 0\n')
    claude.chmod(0o755)
    log = tmp_path / "argv.log"
    out = bash(f"""
find_claude() {{ echo {claude}; }}
uname() {{ echo Linux; }}
command() {{ if [[ "$1" == -v ]]; then case "$2" in chromium|chromium-browser) return 1;; esac; fi; builtin command "$@"; }}
npm() {{ echo "npm called: $*" >&2; return 1; }}
npx() {{ echo "npx called: $*" >&2; return 1; }}
main
""", HOME=str(tmp_path), ARGV_LOG=str(log))
    assert "already registered" in out
    assert "mcp add" not in log.read_text()
