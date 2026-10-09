"""Phase 45: rule-base stage 3 — recommendation from measured metrics + external
Hugging Face metadata (configs/recommendation_rules.yaml, agentmeter/session/stage3.py).

HARD RULE under test: external metadata never changes a score, rank, SAW value or
the efficiency verdict — present, absent or altered, the scored payload is identical.
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.session import stage3
from agentmeter.session.stage3 import Stage3RuleError, evaluate, fetch_model_context, load_rules

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
QWEN, PHI = "Qwen/Qwen2.5-7B-Instruct", "microsoft/Phi-3-mini-4k-instruct"
FETCHED = "2026-10-09T00:00:00+00:00"


def raw(model, **kw):
    """A Hugging Face /api/models/<id> record (only the fields parse_metadata reads)."""
    base = {"safetensors": {"total": 7_600_000_000}, "downloads": 900_000, "likes": 500,
            "cardData": {"license": "apache-2.0"}, "lastModified": "2026-06-01T00:00:00.000Z",
            "pipeline_tag": "text-generation", "gated": False}
    base.update(kw)
    return base


def ctx(**per_model):
    """model_context built from fake HF records (no network)."""
    def fetcher(model_id, token=None):
        r = per_model[model_id]
        if isinstance(r, Exception):
            raise r
        return r
    c = fetch_model_context(list(per_model), fetcher=fetcher, cache=_NoCache(), enabled=True)
    for m in c["models"].values():                       # deterministic ages
        if m.get("fetched_at"):
            m["fetched_at"] = FETCHED
    return c


class _NoCache:
    def get(self, model_id):
        return None

    def set(self, model_id, entry):
        pass


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    """Real mock sessions: a labelled CSV with 2 models and a PCAP with 1 model."""
    from agentmeter.ingest.run import process_csv, process_pcap
    from agentmeter.session.benchmark import run_session
    out = tmp_path_factory.mktemp("s3")
    process_csv(SAMPLE_CSV, name="c", out_root=out, max_flows=10, max_rows=500_000)
    res = {"csv": run_session(out / "c", [QWEN, PHI], provider="mock")}
    try:
        process_pcap(SAMPLE_PCAP, name="p", out_root=out, max_flows=6, max_packets=100_000, max_pool_flows=20_000)
        res["pcap"] = run_session(out / "p", [QWEN], provider="mock")
    except ImportError:
        pass
    return res


def gpu_payload(p, winner=PHI, vram=6100.0):
    """The CSV mock payload as if measured on an L4 with PHI the efficiency winner."""
    q = copy.deepcopy(p)
    q["environment"].update(provider="real", gpu_name="NVIDIA L4", gpu_vram_total_mb=23034)
    for pm in q["per_model"]:
        pm["efficiency"]["end_to_end"]["mean_peak_vram_mb"] = vram
    q["relative_comparison"]["efficiency_verdict"].update(verdict="more_efficient", model=winner)
    return q


def fired(block, rule, model=None):
    return [r for r in block["rules"] if r["rule"] == rule and (model is None or r["model"] == model)]


# ---------------------------------------------------------------------------
# the rule file
# ---------------------------------------------------------------------------
def test_rule_file_loads_with_the_starter_rules():
    rb = load_rules()
    assert rb["source"] == "configs/recommendation_rules.yaml"
    assert [r["id"] for r in rb["rules"]] == ["gated_access", "restrictive_licence", "vram_headroom",
                                               "model_age", "efficiency_consistent_with_size", "low_adoption"]


@pytest.mark.parametrize("bad, msg", [
    ({"rules": []}, "non-empty list"),
    ({"rules": [{"id": "a", "when": [{"field": "hf.x", "op": "nope"}], "note": "n"}]}, "unknown op"),
    ({"rules": [{"id": "a", "when": [{"field": "secret", "op": "truthy"}], "note": "n"}]}, "must start with"),
    ({"rules": [{"id": "a", "when": [{"field": "hf.x", "op": "gt", "value": "$nope"}], "note": "n"}]}, "unknown constant"),
    ({"rules": [{"id": "a", "when": [{"field": "hf.x", "op": "truthy"}], "note": "n"},
                {"id": "a", "when": [{"field": "hf.x", "op": "truthy"}], "note": "n"}]}, "unique id"),
    ({"rules": [{"id": "a", "when": [{"field": "hf.x", "op": "truthy"}], "note": "n", "sources": ["guess"]}]}, "unknown sources"),
])
def test_malformed_rule_files_are_refused(tmp_path, bad, msg):
    f = tmp_path / "r.yaml"
    f.write_text(yaml.safe_dump(bad))
    with pytest.raises(Stage3RuleError, match=msg):
        load_rules(f)


# ---------------------------------------------------------------------------
# each starter rule fires and does not fire
# ---------------------------------------------------------------------------
def test_gated_access(sessions):
    b = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN, gated="manual", cardData={"license": "other"}),
                                        PHI: raw(PHI, gated=False)}))
    assert fired(b, "gated_access", QWEN)[0]["result"] == "fired"
    assert fired(b, "gated_access", PHI)[0]["result"] == "not_fired"
    note = next(n for n in b["notes"] if n["rule"] == "gated_access")
    assert "accept its licence" in note["note"] and note["source_url"] == f"https://huggingface.co/{QWEN}"


def test_restrictive_licence(sessions):
    b = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN, cardData={"license": "cc-by-nc-4.0"}),
                                        PHI: raw(PHI, cardData={"license": "mit"})}))
    assert fired(b, "restrictive_licence", QWEN)[0]["result"] == "fired"
    assert fired(b, "restrictive_licence", PHI)[0]["result"] == "not_fired"
    assert "'cc-by-nc-4.0'" in next(n["note"] for n in b["notes"] if n["rule"] == "restrictive_licence")
    b2 = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN, cardData={"license": "llama3"}), PHI: raw(PHI)}))
    assert fired(b2, "restrictive_licence", QWEN)[0]["result"] == "fired"


def test_vram_headroom(sessions):
    b = evaluate(gpu_payload(sessions["csv"]), ctx(**{QWEN: raw(QWEN), PHI: raw(PHI)}))
    r = fired(b, "vram_headroom", QWEN)[0]
    assert r["result"] == "fired" and set(r["sources"]) == {"measured", "session_gpu", "huggingface"}
    note = next(n["note"] for n in b["notes"] if n["rule"] == "vram_headroom" and n["model"] == QWEN)
    assert "6,100 MB of the NVIDIA L4's 23,034 MB" in note and "16,934 MB (74%) headroom" in note
    # a CPU/mock session has no VRAM reading and no GPU: no data, not a "no"
    b2 = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN), PHI: raw(PHI)}))
    assert fired(b2, "vram_headroom", QWEN)[0]["result"] == "no_data"


def test_model_age(sessions):
    b = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN, lastModified="2024-09-01T00:00:00Z"),
                                        PHI: raw(PHI, lastModified="2026-08-01T00:00:00Z")}))
    assert fired(b, "model_age", QWEN)[0]["result"] == "fired"
    assert fired(b, "model_age", PHI)[0]["result"] == "not_fired"
    assert "25 months" in next(n["note"] for n in b["notes"] if n["rule"] == "model_age")


def test_efficiency_consistent_with_size(sessions):
    c = ctx(**{QWEN: raw(QWEN, safetensors={"total": 7_620_000_000}),
               PHI: raw(PHI, safetensors={"total": 3_820_000_000})})
    b = evaluate(gpu_payload(sessions["csv"], winner=PHI), c)
    assert fired(b, "efficiency_consistent_with_size", PHI)[0]["result"] == "fired"
    assert fired(b, "efficiency_consistent_with_size", QWEN)[0]["result"] == "not_fired"
    assert "3.82 B vs 7.62 B" in next(n["note"] for n in b["notes"] if n["rule"] == "efficiency_consistent_with_size")
    # the bigger model winning, or a tie, does not fire it
    big = evaluate(gpu_payload(sessions["csv"], winner=QWEN), c)
    assert not [n for n in big["notes"] if n["rule"] == "efficiency_consistent_with_size"]
    tie = evaluate(sessions["csv"], c)                                # mock: verdict "tie"
    assert not [n for n in tie["notes"] if n["rule"] == "efficiency_consistent_with_size"]


def test_low_adoption(sessions):
    b = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN, downloads=1200), PHI: raw(PHI, downloads=2_000_000)}))
    assert fired(b, "low_adoption", QWEN)[0]["result"] == "fired"
    assert fired(b, "low_adoption", PHI)[0]["result"] == "not_fired"
    assert "1,200 downloads" in next(n["note"] for n in b["notes"] if n["rule"] == "low_adoption")


def test_verdict_is_listed_first_and_notes_carry_rule_and_source(sessions):
    b = evaluate(sessions["csv"], ctx(**{QWEN: raw(QWEN, gated="auto"), PHI: raw(PHI)}))
    assert b["verdict"]["text"] == sessions["csv"]["relative_comparison"]["verdict"]
    assert b["verdict"]["source"] == "measured"
    for n in b["notes"]:
        assert n["rule"] and n["sources"] and n["note"]
    assert b["rulebase"] == "configs/recommendation_rules.yaml" and "never changes" in b["framing"]


# ---------------------------------------------------------------------------
# offline / unavailable metadata
# ---------------------------------------------------------------------------
def test_unavailable_metadata_is_recorded_and_rules_report_no_data(sessions):
    c = ctx(**{QWEN: OSError("Network is unreachable"), PHI: raw(PHI)})
    assert c["status"] == "partial"
    q = c["models"][QWEN]
    assert q["status"] == "unavailable" and "unreachable" in q["reason"] and q["fetched_at"] is None
    b = evaluate(sessions["csv"], c)
    for rid in ("gated_access", "restrictive_licence", "model_age", "low_adoption"):
        assert fired(b, rid, QWEN)[0]["result"] == "no_data", rid
    off = fetch_model_context([QWEN], enabled=False)
    assert off["status"] == "unavailable" and "disabled" in off["models"][QWEN]["reason"]


def test_session_survives_a_metadata_outage(tmp_path, monkeypatch):
    """The job path: HF unreachable during run_session -> recorded, job still done."""
    from agentmeter.ingest.run import process_csv
    from agentmeter.server import hf_metadata
    from agentmeter.session.benchmark import RESULTS_JSON, run_session
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "on")
    monkeypatch.setattr(hf_metadata, "default_fetcher", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    monkeypatch.setattr("agentmeter.db.appdb.DEFAULT_APP_DB", tmp_path / "app.db")
    process_csv(SAMPLE_CSV, name="o", out_root=tmp_path, max_flows=5, max_rows=500_000)
    run_session(tmp_path / "o", [QWEN], provider="mock")
    res = json.loads((tmp_path / "o" / RESULTS_JSON).read_text())
    assert res["model_context"]["status"] == "unavailable"
    assert "timed out" in res["model_context"]["models"][QWEN]["reason"]
    assert res["recommendation_stage3"]["metadata_status"] == "unavailable"
    assert res["per_model"][0]["saw"]["composite_100"] is not None


def test_model_context_is_stored_in_session_results(tmp_path, monkeypatch):
    from agentmeter.ingest.run import process_csv
    from agentmeter.server import hf_metadata
    from agentmeter.session.benchmark import RESULTS_JSON, run_session
    monkeypatch.setenv("AGENTMETER_HF_METADATA", "on")
    monkeypatch.setattr(hf_metadata, "default_fetcher",
                        lambda model_id, token=None: raw(model_id, gated="manual", downloads=4321))
    monkeypatch.setattr("agentmeter.db.appdb.DEFAULT_APP_DB", tmp_path / "app.db")
    process_csv(SAMPLE_CSV, name="m", out_root=tmp_path, max_flows=5, max_rows=500_000)
    run_session(tmp_path / "m", [QWEN, PHI], provider="mock")
    res = json.loads((tmp_path / "m" / RESULTS_JSON).read_text())
    mc = res["model_context"]
    assert mc["status"] == "ok" and mc["source"] == "Hugging Face Hub API" and "never used" in mc["purpose"]
    for m in (QWEN, PHI):
        e = mc["models"][m]
        assert e["gated"] is True and e["downloads"] == 4321 and e["license"] == "apache-2.0"
        assert e["source_url"] == f"https://huggingface.co/{m}" and e["fetched_at"]
    assert {"gated_access", "low_adoption"} <= set(res["recommendation_stage3"]["fired"])


# ---------------------------------------------------------------------------
# HARD RULE: metadata never alters verdict / SAW / scores / ranks
# ---------------------------------------------------------------------------
SCORED_KEYS = ("per_model", "comparison", "relative_comparison", "phase8", "phase8_efficiency_only",
               "sensitivity", "statistics", "phase7", "per_agent", "session")


def _scored(p):
    return json.dumps({k: p.get(k) for k in SCORED_KEYS}, sort_keys=True, default=str)


def test_metadata_never_changes_verdict_or_saw(sessions):
    base = sessions["csv"]
    before = _scored(base)
    variants = {
        "present": ctx(**{QWEN: raw(QWEN), PHI: raw(PHI)}),
        "absent": fetch_model_context([QWEN, PHI], enabled=False),
        "altered": ctx(**{QWEN: raw(QWEN, gated="manual", downloads=3, safetensors={"total": 1},
                                    cardData={"license": "cc-by-nc-4.0"}, lastModified="2019-01-01T00:00:00Z"),
                          PHI: raw(PHI, safetensors={"total": 99_000_000_000})}),
        "garbage": {"status": "ok", "models": {QWEN: {"status": "ok", "params_b": "not a number",
                                                      "downloads": "many", "gated": "maybe"}}},
    }
    verdicts = set()
    for name, c in variants.items():
        p = copy.deepcopy(base)
        stage3.add_stage3(p, context=c)
        assert _scored(p) == before, name                       # nothing scored changed
        assert set(p) - set(base) <= {"model_context", "recommendation_stage3"}, name
        verdicts.add(p["recommendation_stage3"]["verdict"]["text"])
        # and the engine never mutated its input
        assert _scored(base) == before
    assert verdicts == {base["relative_comparison"]["verdict"]}  # the same measured verdict every time


def test_a_broken_rule_file_never_breaks_the_payload(sessions, tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("rules: [")
    p = copy.deepcopy(sessions["csv"])
    stage3.add_stage3(p, context=fetch_model_context([QWEN], enabled=False), rules_path=f)
    assert "error" in p["recommendation_stage3"] and p["recommendation_stage3"]["verdict"]["text"]
    assert _scored(p) == _scored(sessions["csv"])


# ---------------------------------------------------------------------------
# PDF + the session analysis sections
# ---------------------------------------------------------------------------
def test_pdf_has_stage3_and_the_rules_table(sessions):
    from pypdf import PdfReader

    from agentmeter.session.pdf_report import build_report_pdf
    p = gpu_payload(sessions["csv"])
    stage3.add_stage3(p, context=ctx(**{QWEN: raw(QWEN, gated="manual"), PHI: raw(PHI, downloads=10)}))
    t = " ".join("\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(build_report_pdf(p))).pages).split())
    assert "Recommendation — stage 3" in t and "Measured head-to-head verdict:" in t
    assert "Rules fired" in t and "[gated_access]" in t and "[low_adoption]" in t
    for col in ("Rule", "Condition", "Result", "Source", "Fetched at"):
        assert col in t
    assert "hf.gated truthy" in t and FETCHED[:10] in t and "never changes any score" in t


def test_pdf_states_when_metadata_was_unavailable(sessions):
    from pypdf import PdfReader

    from agentmeter.session.pdf_report import build_report_pdf
    p = copy.deepcopy(sessions["csv"])
    stage3.add_stage3(p, context=fetch_model_context([QWEN, PHI], enabled=False))
    t = " ".join("\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(build_report_pdf(p))).pages).split())
    assert "No context rule fired (Hugging Face metadata unavailable" in t and "no_data" in t


def test_session_results_carry_the_full_analysis_sections(sessions):
    c = sessions["csv"]
    assert c["per_class"]["table"] and c["advanced"]["pareto"] and c["advanced"]["misclass_cost"]
    assert "recommendation_stage3" in c and "model_context" in c
    if "pcap" in sessions:
        p = sessions["pcap"]
        assert "per_class" not in p and "phase7" not in p
        assert p["advanced"]["pareto"] is None and p["advanced"]["misclass_cost"] is None
        assert p["advanced"]["accuracy_note"] == "accuracy not measured for unlabelled input"
        assert p["advanced"]["throughput"]
