"""Phase 22 — HF Hub metadata enrichment tests (CPU, no live network).

The HTTP client is MOCKED throughout (an injected fetcher / monkeypatched default),
so nothing here hits the network. Covers: parsing a sample HF record into the clean
shape; caching (a fresh cache avoids a second fetch); graceful degradation on
429/timeout/offline (unavailable, or stale when a cache exists); and that the cache
path uses the SEPARATE app DB and never opens the locked study DB.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from agentmeter import appdb, hf_metadata as hm

REPO = Path(__file__).resolve().parents[1]

SAMPLE_RAW = {
    "id": "Qwen/Qwen2.5-7B-Instruct",
    "downloads": 1234567,
    "likes": 890,
    "lastModified": "2026-01-15T10:20:30.000Z",
    "pipeline_tag": "text-generation",
    "cardData": {"license": "apache-2.0"},
    "safetensors": {"total": 7615616512, "parameters": {"BF16": 7615616512}},
    "tags": ["text-generation", "license:apache-2.0"],
}


class DictCache:
    """Minimal get/set cache for tests."""
    def __init__(self):
        self.store = {}
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v):
        self.store[k] = v


# --- parsing -----------------------------------------------------------

def test_parse_metadata_shape():
    m = hm.parse_metadata(SAMPLE_RAW, "Qwen/Qwen2.5-7B-Instruct")
    assert m["model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert m["params"] == 7615616512
    assert m["params_b"] == 7.62
    assert m["downloads"] == 1234567
    assert m["likes"] == 890
    assert m["license"] == "apache-2.0"
    assert m["last_modified"].startswith("2026-01-15")
    assert m["pipeline_tag"] == "text-generation"


def test_license_from_tag_fallback():
    raw = {"tags": ["license:mit"]}
    assert hm.parse_metadata(raw, "x/y")["license"] == "mit"
    # missing everything -> None, not a crash
    assert hm.parse_metadata({}, "x/y")["license"] is None
    assert hm.parse_metadata({}, "x/y")["params"] is None


def test_params_from_parameters_sum():
    raw = {"safetensors": {"parameters": {"BF16": 4000000000, "F32": 1000000}}}
    assert hm.parse_metadata(raw, "x/y")["params"] == 4001000000


# --- get_metadata + caching -------------------------------------------

def test_get_metadata_ok_and_caches():
    calls = {"n": 0}
    def fetcher(model_id, token=None):
        calls["n"] += 1
        return SAMPLE_RAW
    cache = DictCache()

    a = hm.get_metadata("Qwen/Qwen2.5-7B-Instruct", fetcher=fetcher, cache=cache, now=1000.0)
    assert a["status"] == "ok" and a["cached"] is False and a["likes"] == 890
    assert calls["n"] == 1

    # second call within TTL -> served from cache, fetcher NOT called again
    b = hm.get_metadata("Qwen/Qwen2.5-7B-Instruct", fetcher=fetcher, cache=cache, now=1500.0)
    assert b["status"] == "ok" and b["cached"] is True
    assert calls["n"] == 1


def test_cache_expires_after_ttl():
    calls = {"n": 0}
    def fetcher(model_id, token=None):
        calls["n"] += 1
        return SAMPLE_RAW
    cache = DictCache()
    hm.get_metadata("m/x", fetcher=fetcher, cache=cache, ttl_seconds=100, now=0.0)
    hm.get_metadata("m/x", fetcher=fetcher, cache=cache, ttl_seconds=100, now=1000.0)
    assert calls["n"] == 2  # stale -> refetched


def test_unavailable_on_fetch_error_no_cache():
    def boom(model_id, token=None):
        raise TimeoutError("429 rate limited")
    out = hm.get_metadata("m/x", fetcher=boom, cache=DictCache())
    assert out["status"] == "unavailable"
    assert out["model"] == "m/x"
    assert "429" in out["error"]


def test_stale_served_when_fetch_fails_but_cache_exists():
    cache = DictCache()
    hm.get_metadata("m/x", fetcher=lambda model_id, token=None: SAMPLE_RAW,
                    cache=cache, ttl_seconds=1, now=0.0)
    def boom(model_id, token=None):
        raise ConnectionError("offline")
    out = hm.get_metadata("m/x", fetcher=boom, cache=cache, ttl_seconds=1, now=10000.0)
    assert out["status"] == "stale" and out["cached"] is True
    assert out["likes"] == 890 and "offline" in out["error"]


def test_get_many_isolates_failures():
    def fetcher(model_id, token=None):
        if model_id == "bad/one":
            raise TimeoutError("timeout")
        return SAMPLE_RAW
    out = hm.get_many(["good/one", "bad/one"], fetcher=fetcher, cache=DictCache())
    assert out[0]["status"] == "ok"
    assert out[1]["status"] == "unavailable"


# --- AppStore cache path never touches the locked study DB -------------

def test_appstore_cache_roundtrip(tmp_path):
    store = appdb.AppStore(tmp_path / "app.db")
    try:
        cache = hm.make_store_cache(store)
        assert cache.get("m/x") is None
        data = hm.parse_metadata(SAMPLE_RAW, "m/x")
        cache.set("m/x", {"fetched_at": 42.0, "status": "ok", "data": data})
        got = cache.get("m/x")
        assert got["status"] == "ok" and got["fetched_at"] == 42.0
        assert got["data"]["likes"] == 890
    finally:
        store.close()


def test_metadata_cache_never_opens_locked_db(tmp_path, monkeypatch):
    real = sqlite3.connect
    opened = []
    def spy(target, *a, **k):
        opened.append(str(target))
        if "agentmeter_full_l4.db" in str(target):
            raise AssertionError("HF metadata cache opened the locked study DB")
        return real(target, *a, **k)
    monkeypatch.setattr(appdb.sqlite3, "connect", spy)

    store = appdb.AppStore(tmp_path / "app.db")
    try:
        cache = hm.make_store_cache(store)
        hm.get_metadata("m/x", fetcher=lambda model_id, token=None: SAMPLE_RAW, cache=cache)
        assert cache.get("m/x")["status"] == "ok"
    finally:
        store.close()
    assert opened and all("agentmeter_full_l4.db" not in p for p in opened)


# --- Flask endpoint (mocked fetcher; graceful) -------------------------

def _load_server():
    path = REPO / "scripts" / "pull_eval_server.py"
    spec = importlib.util.spec_from_file_location("pull_eval_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(tmp_path):
    pytest.importorskip("flask")
    import yaml
    from agentmeter import pull_eval
    srv = _load_server()
    csv = tmp_path / "f.csv"; csv.write_text("A,Label\n1,Benign\n")
    cfg = {"run": {"mode": "pilot", "device": "cpu", "require_gpu": False, "seed": 1, "models": ["mock/m"]},
           "model": {"provider": "mock", "name": "mock/m", "mock": {"latency_s": 0.0}},
           "dataset": {"path": str(csv), "label_column": "Label", "id_column": None,
                       "drop_columns": [], "limit": 1, "max_feature_chars": 100, "label_map": {}, "drop_labels": []},
           "pipeline": {"agents": ["perceive", "reason", "decide", "act"],
                        "max_new_tokens": {"perceive": 8, "reason": 8, "decide": 4, "act": 8}},
           "classes": ["Benign"], "mitre": {"Benign": "N/A"},
           "scoring": {"weights": {"accuracy": 0.4, "latency": 0.25, "vram": 0.2, "tokens": 0.15}},
           "storage": {"sqlite_path": str(tmp_path / "study.db")}}
    base = tmp_path / "cfg.yaml"; base.write_text(yaml.safe_dump(cfg))
    mgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                               out_dir=str(tmp_path / "pulls"))
    app = srv.create_app(mgr, app_db_path=str(tmp_path / "appmeta.db"))
    return app.test_client()


def test_endpoint_single_and_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(hm, "default_fetcher", lambda model_id, token=None, timeout=8.0: SAMPLE_RAW)
    client = _client(tmp_path)

    r = client.get("/api/model-metadata?model=Qwen/Qwen2.5-7B-Instruct")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok" and body["license"] == "apache-2.0"

    rb = client.get("/api/model-metadata")
    assert rb.status_code == 200
    models = rb.get_json()["models"]
    assert len(models) == len(hm.CANONICAL_MODELS)
    assert all(m["status"] == "ok" for m in models)


def test_endpoint_graceful_when_api_down(tmp_path, monkeypatch):
    def boom(model_id, token=None, timeout=8.0):
        raise ConnectionError("offline")
    monkeypatch.setattr(hm, "default_fetcher", boom)
    client = _client(tmp_path)
    r = client.get("/api/model-metadata?model=x/y")
    assert r.status_code == 200                      # never a 500
    assert r.get_json()["status"] == "unavailable"
