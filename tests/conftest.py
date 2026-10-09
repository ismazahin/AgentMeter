"""Suite-wide defaults. Stage 3 (session/stage3.py) fetches Hugging Face metadata when a
benchmark session finishes; tests never hit the live API unless a test opts in by
injecting a fetcher or setting AGENTMETER_HF_METADATA itself."""
import pytest


@pytest.fixture(autouse=True)
def _no_live_hf_metadata(monkeypatch):
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "off")
