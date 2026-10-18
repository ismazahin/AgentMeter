"""Model catalog: extra models users may benchmark next to the 5 study models.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. The
suitability rules only decide whether a model can be benchmarked like the study models
(original weights → 4-bit NF4 at load, on this GPU). No network: Hugging Face is faked.
"""
from __future__ import annotations

import pytest

from agentmeter.server import model_catalog as mc
from test_control_plane import app, auth, tok  # noqa: F401  (app is a fixture)

GB = 1024 ** 3


def repo(model_id, params_b=3.0, *, files=None, tags=(), config=None, gated=False, license_="apache-2.0",
         pipeline="text-generation"):
    """A Hugging Face /api/models/<id>?blobs=true record (only the fields we read)."""
    files = files if files is not None else [("model-00001-of-00002.safetensors", 3 * GB),
                                             ("model-00002-of-00002.safetensors", 3 * GB),
                                             ("config.json", 700), ("tokenizer.json", 7_000_000),
                                             ("original/consolidated.pth", 6 * GB)]
    return {"id": model_id, "safetensors": {"total": int(params_b * 1e9)}, "downloads": 1_000_000, "likes": 50,
            "gated": gated, "pipeline_tag": pipeline, "tags": list(tags) + [f"license:{license_}"],
            "lastModified": "2026-01-01T00:00:00Z", "config": config or {"model_type": "qwen2"},
            "siblings": [{"rfilename": n, "size": s} for n, s in files]}


RULES = mc.load_rules()
ENV24 = {"gpu_vram_gb": 24.0, "disk_free_gb": 200.0, "transformers": "4.46.0"}


def assess(raw, env=ENV24, on_disk=False, native=lambda t: True):
    return mc.assess({"id": raw["id"]}, {"status": "ok", "repo": mc.parse_repo(raw, raw["id"])}, env, RULES,
                     on_disk=on_disk, native=native)


def fired(out):
    return {r["rule"] for r in out["reasons"]}


# ---------------------------------------------------------------------------
# the files
# ---------------------------------------------------------------------------
def test_catalog_and_rules_load():
    cat = mc.load_catalog()
    assert len(cat) >= 5 and all("/" in m["id"] for m in cat)
    from agentmeter.session.benchmark import canonical_models
    assert not {m["id"] for m in cat} & set(canonical_models())          # study models are listed separately
    assert {r["id"] for r in RULES["rules"]} >= {"already_quantized", "remote_code", "exceeds_gpu", "gated"}


def test_a_pre_quantized_repo_cannot_be_listed(tmp_path):
    p = tmp_path / "cat.yaml"
    p.write_text("models:\n  - id: TheBloke/Mistral-7B-Instruct-v0.2-GPTQ\n")
    with pytest.raises(mc.CatalogError, match="original-weights"):
        mc.load_catalog(p)


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def test_a_small_original_weights_model_is_ok():
    out = assess(repo("Qwen/Qwen2.5-3B-Instruct", 3.09))
    assert out["status"] == "ok" and out["available"] and not out["reasons"]
    assert out["vram_4bit_gb"] == pytest.approx(3.09 * 0.55 + 1.5 + 1.0, abs=0.1) and out["fits_gpu"] is True
    assert out["download_gb"] == pytest.approx(6.0, abs=0.1)              # original/*.pth is not downloaded
    assert out["original_format"] == "original weights" and out["quantization"] == "4-bit NF4"


@pytest.mark.parametrize("raw, fmt", [
    (repo("x/m-AWQ-like", config={"model_type": "qwen2", "quantization_config": {"quant_method": "awq"}}), "awq"),
    (repo("x/m-tagged", tags=["gptq"]), "gptq"),
    (repo("x/m-gguf", files=[("m.Q4_K_M.gguf", 4 * GB), ("config.json", 10)]), "gguf"),
])
def test_pre_quantized_repos_are_blocked(raw, fmt):
    out = assess(raw)
    assert out["status"] == "blocked" and "already_quantized" in fired(out)
    assert fmt in next(r["note"] for r in out["reasons"] if r["rule"] == "already_quantized")


def test_remote_code_is_blocked_unless_transformers_supports_it_natively():
    raw = repo("x/custom", tags=["custom_code"], config={"model_type": "brandnew", "auto_map": {"AutoModel": "x"}})
    assert "remote_code" in fired(assess(raw, native=lambda t: False))
    assert "unsupported_architecture" in fired(assess(raw, native=lambda t: False))
    phi = repo("microsoft/Phi-3.5-mini-instruct", 3.8, tags=["custom_code"],
               config={"model_type": "phi3", "auto_map": {"AutoConfig": "x"}})
    assert assess(phi, native=lambda t: True)["status"] == "ok"           # phi3 ships with transformers now
    unknown = assess(phi, native=lambda t: None)                         # no transformers here (demo/CI)
    assert unknown["status"] == "ok" and next(a for a in unknown["audit"] if a["rule"] == "remote_code")["result"] == "no_data"


def test_gpu_fit_uses_the_4bit_estimate():
    big = assess(repo("x/70b", 70.0))
    assert big["status"] == "blocked" and {"too_large", "exceeds_gpu"} <= fired(big)
    tight = assess(repo("x/30b", 30.0), env={**ENV24, "gpu_vram_gb": 24.0})          # ~19 GB of 24 = 0.8
    assert tight["status"] == "ok"
    tight16 = assess(repo("x/14b", 14.7), env={**ENV24, "gpu_vram_gb": 12.0})         # ~10.6 GB of 12
    assert tight16["status"] == "warning" and "tight_gpu" in fired(tight16)
    over = assess(repo("x/14b", 14.7), env={**ENV24, "gpu_vram_gb": 8.0})
    assert over["status"] == "blocked" and over["fits_gpu"] is False


def test_no_gpu_means_no_data_not_a_verdict():
    out = assess(repo("x/m", 7.0), env={"gpu_vram_gb": None, "disk_free_gb": 100.0, "transformers": None})
    assert out["status"] == "ok" and out["fits_gpu"] is None
    assert next(a for a in out["audit"] if a["rule"] == "exceeds_gpu")["result"] == "no_data"


def test_warnings_gated_licence_download_disk():
    out = assess(repo("meta-llama/Llama-3.1-8B-Instruct", 8.0, gated="manual", license_="llama3.1",
                      files=[("model.safetensors", 16 * GB), ("x.safetensors", 14 * GB), ("config.json", 1)]),
                 env={**ENV24, "disk_free_gb": 20.0})
    assert out["status"] == "warning" and out["available"]
    assert {"gated", "custom_licence", "large_download", "low_disk"} <= fired(out)
    on_disk = assess(repo("x/m", 8.0, files=[("a.safetensors", 30 * GB)]), env={**ENV24, "disk_free_gb": 5.0}, on_disk=True)
    assert on_disk["download_gb"] == 0 and not {"large_download", "low_disk"} & fired(on_disk)


def test_not_a_chat_model_and_no_safetensors():
    assert "not_text_generation" in fired(assess(repo("x/emb", pipeline="feature-extraction")))
    out = assess(repo("x/bin", files=[("pytorch_model.bin", 6 * GB), ("config.json", 1)]))
    assert out["status"] == "blocked" and "no_safetensors" in fired(out)


# ---------------------------------------------------------------------------
# fetching (never raises; cached)
# ---------------------------------------------------------------------------
def test_fetch_is_cached_and_degrades():
    calls = []

    def fetcher(mid, token=None):
        calls.append(mid)
        return repo(mid)
    cache = mc.MemoryCache()
    a = mc.fetch_repo("Qwen/Qwen2.5-3B-Instruct", fetcher=fetcher, cache=cache, now=1000.0)
    b = mc.fetch_repo("Qwen/Qwen2.5-3B-Instruct", fetcher=fetcher, cache=cache, now=2000.0)
    assert a["status"] == b["status"] == "ok" and b["cached"] and calls == ["Qwen/Qwen2.5-3B-Instruct"]

    def down(mid, token=None):
        raise OSError("offline")
    stale = mc.fetch_repo("Qwen/Qwen2.5-3B-Instruct", fetcher=down, cache=cache, now=1000.0 + mc.TTL_S + 1)
    assert stale["status"] == "stale" and stale["repo"]
    none = mc.fetch_repo("x/never-seen", fetcher=down, cache=cache)
    assert none["status"] == "unavailable" and none["repo"] is None


def test_build_catalog_unknown_when_hugging_face_is_unreachable():
    def down(mid, token=None):
        raise OSError("offline")
    out = mc.build_catalog(fetcher=down, env=ENV24, on_disk=lambda m: False)
    assert {x["status"] for x in out["extra"]} == {"unknown"}
    assert all(not x["available"] for x in out["extra"])
    assert len(out["study"]) == 5 and all(s["validated"] for s in out["study"])
    assert "4-bit NF4" in out["quantization"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_catalog_endpoint_needs_a_read_token(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mc, "default_fetcher", lambda mid, token=None, timeout=None: repo(mid, 3.0))
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "on")                     # conftest turns it off
    c = app["c"]
    assert c.get("/api/models/catalog").status_code == 401
    r = c.get("/api/models/catalog", headers=auth(tok("read")))
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["extra"] and all(x["status"] in ("ok", "warning") for x in d["extra"])
    assert d["server"]["gpu_vram_gb"] is None                              # mock mode: no GPU
    assert {"id", "status", "reasons", "vram_4bit_gb", "download_gb", "verification", "audit"} <= set(d["extra"][0])
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "off")                    # disabled: listed, not offered
    off = c.get("/api/models/catalog", headers=auth(tok("read"))).get_json()
    assert {x["status"] for x in off["extra"]} == {"unknown"}


# ---------------------------------------------------------------------------
# which models a REAL benchmark may use
# ---------------------------------------------------------------------------
def test_real_jobs_only_take_study_or_listed_models(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from flask import Flask

    from agentmeter.server import jobs_api
    from agentmeter.server.jobs import JobManager
    from agentmeter.session.benchmark import canonical_models

    study, extra = canonical_models()[0], mc.load_catalog()[0]["id"]
    assert mc.job_model_problem([study], on_disk=lambda m: False) is None             # study models: as before
    assert mc.job_model_problem(["evil/anything"])[1] == "model_not_offered"           # no arbitrary ids
    assert mc.job_model_problem([extra], on_disk=lambda m: False)[1:] == ("model_not_downloaded", 409)
    assert mc.job_model_problem([study, extra], on_disk=lambda m: m == extra) is None   # already on this server

    monkeypatch.setattr("agentmeter.server.runtime.model_on_disk", lambda m: False)
    app_ = Flask("t")
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False)
    jobs_api.register_jobs(app_, lambda: mgr, provider="hf", base_config="configs/run_full_l4.yaml")
    c = app_.test_client()
    r = c.post("/api/jobs", json={"run": "csv_runs/x", "models": ["evil/anything"]})
    assert r.status_code == 400 and r.get_json()["code"] == "model_not_offered"
    r = c.post("/api/jobs", json={"run": "csv_runs/x", "models": [extra]})
    assert r.status_code == 409 and r.get_json()["code"] == "model_not_downloaded"
    r = c.post("/api/jobs", json={"run": "csv_runs/x", "models": [study]})
    assert r.get_json()["code"] not in ("model_not_offered", "model_not_downloaded")      # passed the guard


def test_test_repo_fixture_switch(tmp_path, monkeypatch):
    import json
    f = tmp_path / "repos.json"
    f.write_text(json.dumps({m["id"]: repo(m["id"], 3.0) for m in mc.load_catalog()}))
    monkeypatch.setenv(mc.TEST_REPOS_ENV, str(f))
    out = mc.build_catalog(env=ENV24, on_disk=lambda m: False, native=lambda t: True, enabled=True)
    assert {x["status"] for x in out["extra"]} == {"ok"}


def test_the_apps_study_model_list_matches_the_locked_config():
    import json
    import re

    from agentmeter.config import PROJECT_ROOT
    from agentmeter.session.benchmark import canonical_models
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"var STUDY_MODELS = (\[[^\]]*\]);", html)
    assert m and json.loads(m.group(1)) == canonical_models()
