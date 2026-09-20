"""Phase 11 — .env token slot verify suite (CPU, NO real tokens).

  (a) .env (and .env.local) are gitignored — the real token file can't be staged;
  (b) missing HF_TOKEN -> the gated-pull hint logs a clear "set HF_TOKEN" message,
      never crashes, and never echoes a token value;
  (c) load_env reads values from a temp .env (nothing hard-coded), existing env
      wins, and the builtin parser works without python-dotenv.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentmeter import envtools

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- (a) .env is gitignored --------------------------------------------

def test_env_files_are_gitignored():
    gi = (REPO_ROOT / ".gitignore").read_text()
    assert ".env" in gi and ".env.local" in gi

    # git actually ignores them (path need not exist for check-ignore to match)
    for name in (".env", ".env.local"):
        r = subprocess.run(["git", "check-ignore", name], cwd=REPO_ROOT,
                           capture_output=True, text=True)
        assert r.returncode == 0 and name in r.stdout


def test_env_example_is_tracked_and_has_empty_slots():
    # the committed template exists with empty (no-value) slots
    example = (REPO_ROOT / ".env.example").read_text()
    for var in ("HF_TOKEN", "VAST_API_KEY", "VAST_INSTANCE_ID", "GITHUB_TOKEN"):
        assert f"{var}=" in example
        # the template must carry NO real value after the '='
        line = next(l for l in example.splitlines()
                    if l.strip().startswith(f"{var}="))
        assert line.strip() == f"{var}="

    # .env.example is NOT gitignored (must be committable)
    r = subprocess.run(["git", "check-ignore", ".env.example"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    assert r.returncode != 0   # not ignored


# --- (b) missing HF_TOKEN: clear message, no crash, no value leak -------

def test_missing_hf_token_logs_clear_hint(monkeypatch, caplog):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with caplog.at_level("WARNING"):
        msg = envtools.hf_token_hint()
    assert msg is not None and "HF_TOKEN" in msg
    assert "HF_TOKEN" in caplog.text        # logged clearly


def test_present_hf_token_value_never_logged(monkeypatch, caplog):
    secret = "hf_SUPERSECRETVALUE123"
    monkeypatch.setenv("HF_TOKEN", secret)
    with caplog.at_level("INFO"):
        assert envtools.hf_token_hint() is None   # present -> no hint
        envtools.log_token_status()
    # the NAME is logged, the VALUE is never logged
    assert "HF_TOKEN" in caplog.text
    assert secret not in caplog.text


# --- (c) load_env reads a temp .env, existing env wins, builtin parser --

def test_load_env_reads_temp_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FOO_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nFOO_TOKEN=frommfile\nexport BAR_KEY="quoted"\n\n')
    loaded = envtools.load_env(paths=(str(env),))
    assert set(loaded) == {"FOO_TOKEN", "BAR_KEY"}
    import os
    assert os.environ["FOO_TOKEN"] == "frommfile"
    assert os.environ["BAR_KEY"] == "quoted"


def test_existing_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO_TOKEN", "already-set")
    env = tmp_path / ".env"
    env.write_text("FOO_TOKEN=from-dotenv\n")
    loaded = envtools.load_env(paths=(str(env),))
    import os
    assert os.environ["FOO_TOKEN"] == "already-set"   # not overridden
    assert "FOO_TOKEN" not in loaded


def test_builtin_parser_handles_comments_and_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# top\nA=1\n  # indented comment\nB = "two"\nBAD LINE\nC=\n')
    vals = envtools._parse_env_file(env)
    assert vals["A"] == "1" and vals["B"] == "two" and vals["C"] == ""
    assert "BAD LINE" not in vals


def test_load_env_missing_file_is_noop():
    # a non-existent path must not raise and must load nothing
    assert envtools.load_env(paths=("/nonexistent/.env.definitely-not-here",)) == []
