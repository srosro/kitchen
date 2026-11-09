import pytest


@pytest.fixture(autouse=True)
def _private_tmux(tmp_path, monkeypatch):
    """Every tmux socket a test touches lives under its own tmp dir, so a test
    whose mocks miss a call can never reach (or kill) a live kitchen."""
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    # The suite may itself run under `claude -p`, whose hooks kitchen ignores.
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
