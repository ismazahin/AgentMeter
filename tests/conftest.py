"""Suite-wide defaults. Stage 3 (session/stage3.py) fetches Hugging Face metadata when a
benchmark session finishes; tests never hit the live API unless a test opts in by
injecting a fetcher or setting AGENTMETER_HF_METADATA itself.

Set at import time too, so module- and session-scoped fixtures (which run before any
function-scoped autouse fixture) are covered."""
import os

import pytest

os.environ["AGENTMETER_HF_METADATA"] = "off"


@pytest.fixture(autouse=True)
def _no_live_hf_metadata(monkeypatch):
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "off")
