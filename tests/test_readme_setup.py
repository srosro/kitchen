"""The README's plow-chat setup commands, run as written against a stub API."""
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

README = Path(__file__).parent.parent / "README.md"


def readme_block(containing: str) -> str:
    blocks = re.findall(r"```sh\n(.*?)```", README.read_text(), re.S)
    (block,) = [b for b in blocks if containing in b]
    return re.sub(r"^   ", "", block, flags=re.M)


class Conflict(BaseHTTPRequestHandler):
    """Every POST answers 409, as /v1/agents does when the line already has an agent."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(409)
        self.end_headers()
        self.wfile.write(b'{"detail": {"code": "AGENT_EXISTS"}}')

    def log_message(self, *args):
        pass


def test_a_refused_agent_create_keeps_the_account_token_for_a_retry(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), Conflict)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "account.header").write_text("Authorization: Bearer acct_fake\n")
    (tmp_path / "activation.json").write_text("{}")
    try:
        script = f"API=http://127.0.0.1:{server.server_port}\nLINE=ln_fake\n" + readme_block("/v1/agents")
        result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    finally:
        server.shutdown()
    assert "account.header kept" in result.stdout
    assert (tmp_path / "account.header").exists()
    assert not (tmp_path / "token").exists(), "an empty token file would pass the mode check and 401 forever"
