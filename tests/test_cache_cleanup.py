"""Opt-in HF model-cache cleanup tests (no GPU, no network, no real hub).

A fake `huggingface_hub` module is injected into sys.modules so the worker's lazy
`from huggingface_hub import scan_cache_dir` picks it up — the tests run anywhere.
"""
from __future__ import annotations

import sys
import types

import pytest

from agentmeter import worker

TARGET = "mistralai/Mistral-7B-Instruct-v0.3"
OTHER = "meta-llama/Meta-Llama-3-8B-Instruct"


# --- fake huggingface_hub cache objects --------------------------------

class _Rev:
    def __init__(self, h): self.commit_hash = h


class _Repo:
    def __init__(self, repo_id, repo_type, hashes):
        self.repo_id = repo_id
        self.repo_type = repo_type
        self.revisions = [_Rev(h) for h in hashes]


class _Strategy:
    def __init__(self, hashes):
        self.expected_freed_size = 7_000_000_000
        self.hashes = list(hashes)
        self.executed = False

    def execute(self):
        self.executed = True


class _Cache:
    def __init__(self, repos, sink, raise_on_delete=False):
        self.repos = repos
        self._sink = sink
        self._raise = raise_on_delete

    def delete_revisions(self, *hashes):
        if self._raise:
            raise RuntimeError("boom")
        self._sink["called_with"] = list(hashes)
        s = _Strategy(hashes)
        self._sink["strategy"] = s
        return s


def _install_fake_hub(monkeypatch, repos, sink, raise_on_delete=False):
    cache = _Cache(repos, sink, raise_on_delete)
    mod = types.ModuleType("huggingface_hub")
    mod.scan_cache_dir = lambda: cache
    monkeypatch.setitem(sys.modules, "huggingface_hub", mod)
    return cache


# --- the actual deletion routine ---------------------------------------

def test_deletes_only_the_workers_own_model(monkeypatch):
    sink = {}
    repos = [
        _Repo(TARGET, "model", ["a1", "a2"]),      # <- the one to delete
        _Repo(OTHER, "model", ["b1"]),             # another model: must survive
        _Repo(TARGET, "dataset", ["d1"]),          # same id but a DATASET: must survive
    ]
    _install_fake_hub(monkeypatch, repos, sink)

    res = worker._hf_cache_cleanup(TARGET)
    assert res["deleted"] is True
    # ONLY the target model's revisions were deleted — not the other model, not the dataset.
    assert sink["called_with"] == ["a1", "a2"]
    assert sink["strategy"].executed is True


def test_absent_repo_is_noop_not_crash(monkeypatch):
    sink = {}
    repos = [_Repo(OTHER, "model", ["b1"]), _Repo("some/dataset", "dataset", ["d1"])]
    _install_fake_hub(monkeypatch, repos, sink)

    res = worker._hf_cache_cleanup("microsoft/Phi-3-mini-4k-instruct")
    assert res == {"repo_id": "microsoft/Phi-3-mini-4k-instruct", "deleted": False, "freed_bytes": 0}
    assert "called_with" not in sink  # delete_revisions never called


def test_never_matches_a_dataset_with_the_same_id(monkeypatch):
    sink = {}
    repos = [_Repo(TARGET, "dataset", ["d1"])]   # only a dataset carries the target id
    _install_fake_hub(monkeypatch, repos, sink)

    res = worker._hf_cache_cleanup(TARGET)
    assert res["deleted"] is False
    assert "called_with" not in sink


def test_cleanup_failure_is_swallowed(monkeypatch):
    sink = {}
    repos = [_Repo(TARGET, "model", ["a1"])]
    _install_fake_hub(monkeypatch, repos, sink, raise_on_delete=True)

    # Must NOT raise — returns None and logs a warning; the run continues.
    assert worker._hf_cache_cleanup(TARGET) is None


def test_missing_hub_is_skipped(monkeypatch):
    # No huggingface_hub importable -> skip cleanly, no crash.
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # import -> ImportError
    assert worker._hf_cache_cleanup(TARGET) is None


# --- the config gate ---------------------------------------------------

class _Cfg:
    def __init__(self, val): self.val = val

    def get(self, key, default=None):
        return self.val if key == "run.cleanup_model_cache_after" else default


def test_flag_off_never_invokes_cleanup(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(worker, "_hf_cache_cleanup", lambda mid: called.__setitem__("n", called["n"] + 1))
    assert worker.maybe_cleanup_model_cache(_Cfg(False), TARGET) is None
    assert called["n"] == 0  # OFF (default) -> routine never called


def test_flag_on_invokes_cleanup_with_the_model_id(monkeypatch):
    seen = {}
    def _rec(mid):
        seen["mid"] = mid
        return {"repo_id": mid, "deleted": True}
    monkeypatch.setattr(worker, "_hf_cache_cleanup", _rec)
    out = worker.maybe_cleanup_model_cache(_Cfg(True), TARGET)
    assert seen["mid"] == TARGET
    assert out == {"repo_id": TARGET, "deleted": True}
