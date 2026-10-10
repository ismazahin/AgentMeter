"""Phase 46: the additive efficiency analyses, the decision helper, cost/energy, Compare,
Leaderboard, the chained wizard job and the session API.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product. None of
this may change a score, rank, SAW value or verdict (see also test_scored_keys_golden.py).
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import sys
import types
from pathlib import Path

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT, load_config
from agentmeter.pipeline.agents import HANDOFFS
from agentmeter.server import sessions
from agentmeter.server.jobs import JobManager
from agentmeter.session import analyses, constraints, price
from agentmeter.session.analyses import (bootstrap_mean_diff_ci, cliffs_delta, cliffs_magnitude,
                                         cost_per_1k_flows, integrate_energy_wh, percentiles)
from agentmeter.session.benchmark import prepare_session
from agentmeter.session.scoring import score_session

FIXTURE = Path(__file__).parent / "fixtures" / "golden_session"
MODELS = ["Qwen/Qwen2.5-7B-Instruct", "microsoft/Phi-3-mini-4k-instruct"]
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
SCORED = ["per_model", "comparison", "relative_comparison", "phase8", "phase8_efficiency_only",
          "sensitivity", "statistics", "phase7", "per_agent", "per_class", "advanced", "session"]


# ---------------------------------------------------------------------------
# fixtures: the golden mock session, scored + analysed
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    run = tmp_path_factory.mktemp("g46") / "golden"
    shutil.copytree(FIXTURE, run)
    g = json.loads((run / "golden_session_results.json").read_text())
    plan = prepare_session(run, MODELS, provider="mock")
    payload = score_session(plan, g["session"]["run_id"])
    payload["environment"] = g["environment"]
    return {"run": run, "plan": plan, "run_id": g["session"]["run_id"], "payload": payload,
            "cfg": load_config(plan["config_path"]).data}


def analysed(golden, **kw):
    p = copy.deepcopy(golden["payload"])
    analyses.add_analyses(p, db_path=golden["plan"]["db_path"], run_id=golden["run_id"],
                          run_dir=golden["run"], config_data=golden["cfg"], **kw)
    return p


def real_payload(golden, **kw):
    """The golden session as if measured on an L4 (environment real)."""
    p = copy.deepcopy(golden["payload"])
    p["environment"] = {**p["environment"], "provider": "real", "gpu_name": "NVIDIA L4"}
    analyses.add_analyses(p, db_path=golden["plan"]["db_path"], run_id=golden["run_id"],
                          run_dir=golden["run"], config_data=golden["cfg"], **kw)
    return p


# ---------------------------------------------------------------------------
# D. small statistics
# ---------------------------------------------------------------------------
def test_percentiles_are_linear_interpolated():
    xs = list(range(1, 101))                       # 1..100
    assert percentiles(xs) == {"p50": 50.5, "p95": pytest.approx(95.05), "p99": pytest.approx(99.01)}
    assert percentiles([3.0]) == {"p50": 3.0, "p95": 3.0, "p99": 3.0}
    assert percentiles([]) == {"p50": None, "p95": None, "p99": None}
    assert percentiles([1, None, float("nan"), 3])["p50"] == 2.0


def test_cliffs_delta_and_magnitude():
    assert cliffs_delta([1, 2, 3], [4, 5, 6]) == -1.0
    assert cliffs_delta([4, 5, 6], [1, 2, 3]) == 1.0
    assert cliffs_delta([1, 2], [1, 2]) == 0.0
    assert cliffs_delta([1, 2, 3, 4], [2, 3]) == pytest.approx((3 - 3) / 8)   # 3 wins, 3 losses, 2 ties
    assert cliffs_delta([], [1]) is None
    assert [cliffs_magnitude(d) for d in (0.1, -0.2, 0.4, -0.9, None)] == \
        ["negligible", "small", "medium", "large", None]


def test_bootstrap_ci_is_seeded_and_brackets_the_difference():
    a, b = [10, 11, 12, 13, 14, 15], [5, 6, 7, 8, 9, 10]
    c1, c2 = bootstrap_mean_diff_ci(a, b), bootstrap_mean_diff_ci(a, b)
    assert c1 == c2                                                   # fixed seed -> reproducible
    assert c1["diff"] == 5.0 and c1["ci_low"] < 5.0 < c1["ci_high"] and c1["ci_low"] > 0
    assert bootstrap_mean_diff_ci(a, b, seed=1) != c1                 # the seed is what fixes it
    same = bootstrap_mean_diff_ci([2, 2, 2], [2, 2, 2])
    assert same == {"diff": 0.0, "ci_low": 0.0, "ci_high": 0.0}
    assert bootstrap_mean_diff_ci([1], [2])["diff"] is None           # too few to resample


# ---------------------------------------------------------------------------
# B. cost and energy
# ---------------------------------------------------------------------------
def test_cost_per_1k_flows():
    assert cost_per_1k_flows(2.0, 1.0) == pytest.approx(2000 / 3600)
    assert cost_per_1k_flows(None, 1.0) is None and cost_per_1k_flows(1.0, None) is None


def test_energy_integration():
    flat = [(t, 100.0) for t in range(0, 37)]                         # 100 W for 36 s = 1 Wh
    assert integrate_energy_wh(flat, 0, 36) == pytest.approx(1.0)
    ramp = [(0, 0.0), (10, 100.0)]                                    # linear: mean 50 W x 10 s
    assert integrate_energy_wh(ramp, 0, 10) == pytest.approx(500 / 3600)
    assert integrate_energy_wh(ramp + [(5, 50.0)], 2.5, 7.5) == pytest.approx(5 * 50 / 3600)   # edges interpolated
    assert integrate_energy_wh([(0, 1.0)], 0, 10) is None             # one sample is not a power curve
    assert integrate_energy_wh(flat, 5, 5) is None
    assert integrate_energy_wh(flat, 0, 60) is None                   # samples stop at 36 s: no extrapolation
    assert integrate_energy_wh(flat, 0.0004, 0.0009) == pytest.approx(100 * 0.0005 / 3600)   # inside one gap


def test_mock_run_reports_cost_and_energy_as_not_measured(golden):
    ce = analysed(golden, price={"usd_per_hour": 0.5, "source": "config"})["cost_energy"]
    for r in ce["per_model"]:
        assert r["cost_per_1k_flows_usd"] is None and r["cost_status"] == "not measured"
        assert r["wh_per_flow"] is None and r["energy_status"] == "not measured"


def test_real_run_cost_and_energy(golden):
    win = analyses.model_windows(analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])[0],
                                 {pm["model"]: pm["db_label"] for pm in golden["payload"]["per_model"]})
    t0 = min(w[0] for w in win.values()) - 5
    t1 = max(w[1] for w in win.values()) + 5
    samples = [(t0 + i * 0.5, 72.0) for i in range(int((t1 - t0) / 0.5) + 1)]
    p = real_payload(golden, price={"usd_per_hour": 0.8, "source": "vast_api", "fetched_at": "2026-10-10T00:00:00+00:00"},
                     energy={"method": "nvml", "interval_s": 0.5, "n_samples": len(samples), "samples": samples})
    ce = p["cost_energy"]
    assert ce["price"]["source"] == "vast_api" and ce["energy_sampling"]["interval_s"] == 0.5
    for r, pm in zip(ce["per_model"], p["per_model"]):
        lat = pm["efficiency"]["end_to_end"]["mean_latency_s"]
        assert r["cost_per_1k_flows_usd"] == pytest.approx(lat * 1000 / 3600 * 0.8)
        assert r["energy_status"] == "measured" and r["mean_power_w"] == pytest.approx(72.0)
        assert r["wh_per_flow"] == pytest.approx(72.0 * r["window_s"] / 3600 / r["n_flows"])
        assert r["wh_per_1k_flows"] == pytest.approx(r["wh_per_flow"] * 1000)
    # no price / no samples on a real run -> no_data with the reason, never a number
    p2 = real_payload(golden)
    assert all(r["cost_status"] == "no_data" and r["energy_status"] == "no_data" for r in p2["cost_energy"]["per_model"])


def test_price_sources(monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "secret-key-123")
    monkeypatch.setenv("VAST_INSTANCE_ID", "4242")
    seen = {}

    def fetch(iid, key):
        seen.update(iid=iid, key=key)
        return {"instances": {"id": 4242, "dph_total": 0.412}}
    out = price.resolve_price(0.3, fetcher=fetch)
    assert out["usd_per_hour"] == 0.412 and out["source"] == "vast_api" and out["instance_id"] == "4242"
    assert out["fetched_at"] and seen == {"iid": "4242", "key": "secret-key-123"}
    assert "secret-key-123" not in json.dumps(out)

    def boom(iid, key):
        raise OSError(f"401 for token {key}")
    out = price.resolve_price(0.3, fetcher=boom)
    assert out["usd_per_hour"] == 0.3 and out["source"].startswith("config") and "Vast API" in out["error"]
    assert "secret-key-123" not in json.dumps(out)                    # the key never leaks via an error
    monkeypatch.delenv("VAST_API_KEY")
    none = price.resolve_price(None)
    assert none["usd_per_hour"] is None and "pricing.gpu_usd_per_hour" in none["reason"]


def test_power_sampler(monkeypatch):
    from agentmeter.session.power import PowerSampler
    fake = types.SimpleNamespace(nvmlInit=lambda: None, nvmlDeviceGetHandleByIndex=lambda i: "h",
                                 nvmlDeviceGetPowerUsage=lambda h: 71500)          # mW
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    s = PowerSampler(interval_s=0.05)
    assert s.start() and s.method == "nvml"
    import time
    time.sleep(0.3)
    r = s.stop()
    assert r["n_samples"] >= 3 and {w for _, w in r["samples"]} == {71.5} and r["interval_s"] == 0.05
    # no NVML and no nvidia-smi: nothing sampled, a reason instead
    monkeypatch.setitem(sys.modules, "pynvml", None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    s2 = PowerSampler()
    assert s2.start() is False and "no GPU power source" in s2.error


# ---------------------------------------------------------------------------
# A. agents
# ---------------------------------------------------------------------------
def test_agents_block(golden):
    ag = analysed(golden)["agents"]
    assert ag["agent_order"] == ["perceive", "reason", "decide", "act"] and ag["handoffs"] == HANDOFFS
    sr, am = analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])
    for m in ag["per_model"]:
        dbm = next(pm["db_label"] for pm in golden["payload"]["per_model"] if pm["model"] == m["model"])
        assert sum(a["latency_share"] for a in m["agents"]) == pytest.approx(1.0)
        assert sum(a["token_share"] for a in m["agents"]) == pytest.approx(1.0)
        rows = [r for r in am if r["model"] == dbm]
        flows = {r["scenario_id"] for r in rows}
        out = {(r["scenario_id"], r["agent_name"]): r["output_tokens"] for r in rows}
        handoff = sum(out[(sid, src)] for sid in flows for ag_, srcs in HANDOFFS.items() for src in srcs)
        assert m["overhead"]["handoff_tokens_per_flow"] == pytest.approx(handoff / len(flows))
        tot = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
        assert m["overhead"]["handoff_share_of_total"] == pytest.approx(handoff / tot)
        # timeline: back-to-back, cumulative, sums to the flow's end-to-end time
        for sid, segs in m["timeline"].items():
            assert [s[0] for s in segs] == ag["agent_order"]
            for a_, b_ in zip(segs, segs[1:]):
                assert b_[1] == pytest.approx(a_[1] + a_[2], abs=2e-6)
            e2e = next(r["scenario_total_time_s"] for r in sr if r["model"] == dbm and r["scenario_id"] == sid)
            assert segs[-1][1] + segs[-1][2] == pytest.approx(e2e, abs=1e-5)
        decide = next(a for a in m["agents"] if a["agent"] == "decide")
        assert decide["failures"]["unparseable_label"] == 0 and decide["failures"]["token_cap"] == 12
        assert all(a["failures"]["retries"] == 0 and a["failures"]["timeouts"] is None for a in m["agents"])
        assert all(a["mean_peak_vram_mb"] is None for a in m["agents"])          # mock: never fabricated


def test_agent_failure_counts():
    am = [{"model": "m", "scenario_id": f"s{i}", "agent_name": ag, "wall_time_s": 0.1, "ttft_s": None,
           "vram_delta_mb": None, "input_tokens": 10, "output_tokens": out}
          for i, outs in enumerate([(5, 12), (0, 3)]) for ag, out in zip(("perceive", "decide"), outs)]
    sr = [{"model": "m", "scenario_id": "s0", "predicted_label": "Benign"},
          {"model": "m", "scenario_id": "s1", "predicted_label": "Unparseable"}]
    b = analyses.agents_block(sr, am, {"m": "m"}, {"perceive": 160, "decide": 12})
    per, dec = b["per_model"][0]["agents"]
    assert per["failures"]["empty_output"] == 1 and per["failure_rate"] == 0.5
    assert dec["failures"]["hit_token_cap"] == 1 and dec["cap_rate"] == 0.5
    assert dec["failures"]["unparseable_label"] == 1


# ---------------------------------------------------------------------------
# D. latency distribution + effect sizes
# ---------------------------------------------------------------------------
def test_latency_block_and_warmup():
    sr = ([{"model": "a", "scenario_total_time_s": 3.0}] * 5 + [{"model": "a", "scenario_total_time_s": 1.0}] * 20 +
          [{"model": "b", "scenario_total_time_s": 1.0}] * 25)
    ld = analyses.latency_block(sr, {"A": "a", "B": "b"})
    a, b = ld["per_model"]
    assert a["warmup"]["warmup_effect"] is True and a["warmup"]["ratio"] == 3.0
    assert b["warmup"]["warmup_effect"] is False and "within 20%" in b["warmup"]["note"]
    assert a["p50"] == 1.0 and a["p99"] == pytest.approx(3.0)
    assert sum(a["histogram"]) == 25 and len(ld["bin_edges_s"]) == analyses.HIST_BINS + 1
    assert ld["small_sample_note"] is None                           # 25 >= 20 per model
    tiny = analyses.latency_block(sr[:6], {"A": "a"})
    assert tiny["per_model"][0]["warmup"]["warmup_effect"] is None and "Too few flows" in tiny["per_model"][0]["warmup"]["note"]
    assert "too small" in tiny["small_sample_note"]


def test_effect_sizes(golden):
    p = analysed(golden)
    ef = p["effect_sizes"]
    lat = next(m for m in ef["metrics"] if m["metric"] == "latency_s")
    sr, _ = analyses.read_rows(golden["plan"]["db_path"], golden["run_id"])
    lab = {pm["model"]: pm["db_label"] for pm in p["per_model"]}
    xa = [r["scenario_total_time_s"] for r in sr if r["model"] == lab[ef["model_a"]]]
    xb = [r["scenario_total_time_s"] for r in sr if r["model"] == lab[ef["model_b"]]]
    assert lat["cliffs_delta"] == cliffs_delta(xa, xb)
    assert lat["kruskal_wallis_p"] == p["statistics"]["scenario_total_time_s"]["p_value"]   # existing p, not recomputed
    assert next(m for m in ef["metrics"] if m["metric"] == "peak_vram_mb")["available"] is False
    assert "too small" in ef["small_sample_note"]
    one = analyses.effect_block(sr, [], {"A": lab[ef["model_a"]]})
    assert "two models" in one["skipped"]


# ---------------------------------------------------------------------------
# C. decision helper (constraint rules)
# ---------------------------------------------------------------------------
def test_constraint_rule_file_loads():
    rb = constraints.load_rules()
    assert rb["source"] == "configs/constraint_rules.yaml"
    assert [r["id"] for r in rb["rules"]] == ["mean_latency", "p95_latency", "peak_vram", "cost_per_1k_flows"]
    assert set(rb["inputs"]) == {"max_mean_latency_s", "max_p95_latency_s", "max_peak_vram_mb", "max_cost_per_1k_flows_usd"}


@pytest.mark.parametrize("bad, msg", [
    ({"inputs": {"x": {}}, "rules": [{"id": "a", "field": "measured.mean_latency_s", "op": "eq", "limit": "x"}]}, "unknown op"),
    ({"inputs": {"x": {}}, "rules": [{"id": "a", "field": "measured.nope", "op": "le", "limit": "x"}]}, "unknown field"),
    ({"inputs": {"x": {}}, "rules": [{"id": "a", "field": "measured.mean_latency_s", "op": "le", "limit": "y"}]}, "not declared"),
    ({"inputs": {}, "rules": []}, "inputs"),
])
def test_malformed_constraint_files_are_refused(tmp_path, bad, msg):
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(bad))
    with pytest.raises(constraints.ConstraintError, match=msg):
        constraints.load_rules(f)


def test_constraints_meets_fails_no_data_and_not_set(golden):
    p = analysed(golden)
    lat = {pm["model"]: pm["efficiency"]["end_to_end"]["mean_latency_s"] for pm in p["per_model"]}
    hi = max(lat.values())
    assert all(m["result"] == "no_constraints" for m in p["decision_helper"]["per_model"])     # defaults: none set
    d = constraints.evaluate_constraints(p, {"max_mean_latency_s": hi * 10})
    assert all(m["result"] == "meets" for m in d["per_model"])
    assert all(r["result"] == "not_set" for m in d["per_model"] for r in m["rows"] if r["rule"] != "mean_latency")
    d = constraints.evaluate_constraints(p, {"max_mean_latency_s": min(lat.values()) / 10})
    assert all(m["result"] == "fails" and m["failed"] == ["mean_latency"] for m in d["per_model"])
    d = constraints.evaluate_constraints(p, {"max_mean_latency_s": hi * 10, "max_peak_vram_mb": "8000",
                                             "max_cost_per_1k_flows_usd": "1"})
    for m in d["per_model"]:                                          # mock: no VRAM, no cost -> no_data
        assert m["result"] == "no_data" and m["no_data"] == ["peak_vram", "cost_per_1k_flows"]
    d = constraints.evaluate_constraints(p, {"max_mean_latency_s": min(lat.values()) / 10, "max_peak_vram_mb": 1})
    assert all(m["result"] == "fails" for m in d["per_model"])        # a failure outranks no_data
    with pytest.raises(constraints.ConstraintError):
        constraints.evaluate_constraints(p, {"max_mean_latency_s": "-1"})
    with pytest.raises(constraints.ConstraintError):
        constraints.evaluate_constraints(p, {"max_mean_latency_s": "fast"})


def test_constraints_on_a_real_run_without_total_peak_is_no_data(golden):
    """Working VRAM alone (a session recorded before total peak existed) never satisfies
    the VRAM limit: the limit is on TOTAL peak, so it is no_data — never "meets"."""
    p = real_payload(golden, price={"usd_per_hour": 1.0, "source": "config"})
    for ag in p["agents"]["per_model"]:                               # working VRAM readings only
        for i, a in enumerate(ag["agents"]):
            a["max_peak_vram_mb"] = 1000.0 + 100 * i
    d = constraints.evaluate_constraints(p, {"max_peak_vram_mb": 99999, "max_cost_per_1k_flows_usd": 10})
    for m in d["per_model"]:
        vr = next(r for r in m["rows"] if r["rule"] == "peak_vram")
        assert vr["value"] is None and vr["result"] == "no_data" and m["result"] == "no_data"
        assert next(r for r in m["rows"] if r["rule"] == "cost_per_1k_flows")["result"] == "meets"
    assert all(m["status"] == "no_data" and "not recorded" in m["reason"] for m in p["memory"]["per_model"])


def test_new_blocks_never_touch_scored_keys(golden):
    before = {k: json.dumps(golden["payload"].get(k), sort_keys=True) for k in SCORED}
    p = real_payload(golden, price={"usd_per_hour": 9.9, "source": "config"},
                     energy={"samples": [(0, 1e6), (1e12, 1e6)], "method": "nvml", "interval_s": 0.5})
    constraints.evaluate_constraints(p, {"max_mean_latency_s": 0})
    for k in SCORED:
        assert json.dumps(p.get(k), sort_keys=True) == before[k], k


# ---------------------------------------------------------------------------
# Compare validity + Leaderboard grouping
# ---------------------------------------------------------------------------
def _entry(jid, sha, gpu, fp, lat, n=10, models=("m1", "m2"), provider="real", settings=None):
    res = {"session": {"n_flows": n, "source_type": "csv", "accuracy_available": False},
           "environment": {"provider": provider, "gpu_name": gpu},
           "session_identity": {"prepared_set_sha256": sha, "gpu_label": gpu if provider == "real" else "Demo (mock, no GPU)",
                                "provider": provider, "settings_fingerprint": fp, "settings": settings or {"k": fp}},
           "per_model": [{"model": m, "efficiency": {"n_flows": n, "end_to_end": {"mean_latency_s": lat * (i + 1),
                                                                                   "mean_peak_vram_mb": None,
                                                                                   "mean_tokens_per_flow": 100.0}}}
                         for i, m in enumerate(models)]}
    return {"job_id": jid, "status": "done", "models": list(models)}, res


def test_compare_validity():
    a, b = _entry("A", "s1", "NVIDIA L4", "f1", 1.0), _entry("B", "s1", "NVIDIA L4", "f1", 2.0)
    v = sessions.validity(a[0], a[1], b[0], b[1])
    assert v["like_for_like"] and v["label"] == "like-for-like" and not v["differences"]
    c = _entry("C", "s2", "NVIDIA A100", "f2", 2.0, settings={"k": "f2", "pipeline.max_new_tokens": 9})
    v = sessions.validity(a[0], a[1], c[0], c[1])
    assert not v["like_for_like"] and v["label"] == "not like-for-like"
    assert [k["check"] for k in v["checks"] if not k["same"]] == ["prepared_set", "gpu", "settings"]
    assert next(k for k in v["checks"] if k["check"] == "settings")["differs"] == ["k", "pipeline.max_new_tokens"]
    cmp_ = sessions.compare(a[0], a[1], b[0], b[1])
    lat = next(r for r in cmp_["rows"] if r["metric"] == "mean_latency_s")
    assert lat["values"] == [1.0, 2.0, 2.0, 4.0] and lat["best_index"] == [0]
    assert {r["metric"] for r in cmp_["rows"]} >= {k for k, *_ in sessions.METRICS}
    assert "NOT like-for-like" in sessions.compare(a[0], a[1], c[0], c[1])["note"]


def test_leaderboard_ranks_only_within_a_group():
    e = [_entry("A", "s1", "NVIDIA L4", "f1", 1.0, n=10), _entry("B", "s1", "NVIDIA L4", "f1", 3.0, n=30),
         _entry("C", "s2", "NVIDIA L4", "f1", 0.1), _entry("D", "s1", "NVIDIA A100", "f1", 0.5)]
    lb = sessions.leaderboard(e, "latency")
    assert len(lb["groups"]) == 3                                      # (s1,L4), (s2,L4), (s1,A100)
    g = next(g for g in lb["groups"] if g["prepared_set_sha256"] == "s1" and g["gpu"] == "NVIDIA L4")
    assert g["n_sessions"] == 2 and not g["mixed_settings"]
    m1 = next(r for r in g["rows"] if r["model"] == "m1")
    assert m1["n_sessions"] == 2 and m1["n_flows"] == 40
    assert m1["mean_latency_s"] == pytest.approx((1.0 * 10 + 3.0 * 30) / 40)   # flow-weighted
    assert [r["rank"] for r in g["rows"]] == [1, 2]
    for grp in lb["groups"]:                                           # ranks restart in every group
        assert [r["rank"] for r in grp["rows"]] == list(range(1, len(grp["rows"]) + 1))
    vr = sessions.leaderboard(e, "vram")
    assert all(r["rank"] is None for grp in vr["groups"] for r in grp["rows"])   # no value -> unranked
    mixed = sessions.leaderboard([_entry("A", "s1", "L4", "f1", 1.0), _entry("B", "s1", "L4", "f9", 1.0)])
    assert mixed["groups"][0]["mixed_settings"] is True


def test_identity_of_an_old_session_is_computed_from_its_run_dir(golden):
    p = analysed(golden)
    job = {"job_id": "job_x", "run_dir": str(golden["run"]), "environment": p["environment"]}
    old = {k: v for k, v in p.items() if k != "session_identity"}
    assert sessions.identity(job, old) == p["session_identity"]
    assert p["session_identity"]["prepared_set_sha256"] == analyses.prepared_set_sha256(golden["run"])


# ---------------------------------------------------------------------------
# the API: chained wizard job, sessions, compare, leaderboard, constraints, PDF
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app(tmp_path_factory):
    pytest.importorskip("flask")
    tmp = tmp_path_factory.mktemp("api46")
    spec = importlib.util.spec_from_file_location("serve46", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    mgr = JobManager(jobs_dir=tmp / "jobs", results_root=tmp / "results", gpu_available=lambda: False)
    client = srv.create_app(local_results_dir=str(tmp / "results"), job_manager=mgr).test_client()

    def wizard(path, models, flows):
        r = client.post("/api/prepare", data={"file": (io.BytesIO(path.read_bytes()), path.name),
                                              "max_flows": str(flows)}, content_type="multipart/form-data")
        assert r.status_code == 202
        prep = r.get_json()["job_id"]
        b = client.post("/api/jobs", json={"after_prepare": prep, "models": models})   # before the data is ready
        assert b.status_code == 202, b.get_json()
        jid = b.get_json()["job_id"]
        assert mgr.wait(jid, timeout=180)["status"] == "done"
        return prep, jid
    p1, j1 = wizard(SAMPLE_CSV, MODELS, 10)
    p2, j2 = wizard(SAMPLE_PCAP, MODELS[:1], 6)
    run1 = mgr.get(j1)["run_name"]
    j3 = client.post("/api/jobs", json={"run": run1, "models": MODELS}).get_json()["job_id"]
    assert mgr.wait(j3, timeout=180)["status"] == "done"
    return {"c": client, "mgr": mgr, "j1": j1, "j2": j2, "j3": j3, "p1": p1, "run1": run1, "srv": srv, "tmp": tmp}


def test_chained_job_runs_after_its_prepare_step(app):
    j = app["mgr"].get(app["j1"])
    assert j["after_prepare"] == app["p1"] and j["run_name"] == app["run1"] and j["n_flows"] == 10
    res = app["c"].get(f"/api/jobs/{app['j1']}/result").get_json()
    for k in ("agents", "latency_distribution", "effect_sizes", "cost_energy", "decision_helper", "session_identity"):
        assert k in res, k
    assert res["session_identity"]["gpu_label"] == "Demo (mock, no GPU)"


def test_chained_job_fails_cleanly_when_the_data_does_not_prepare(app):
    c, mgr = app["c"], app["mgr"]
    r = c.post("/api/prepare", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "bad.csv"), "max_flows": "5"},
               content_type="multipart/form-data")
    prep = r.get_json()["job_id"]
    b = c.post("/api/jobs", json={"after_prepare": prep, "models": MODELS[:1]})
    if b.status_code == 409:                                          # the prepare already failed
        assert b.get_json()["code"] == "prepare_failed"
        return
    job = mgr.wait(b.get_json()["job_id"], timeout=60)
    assert job["status"] == "failed" and "data preparation failed" in job["error"]
    assert c.post("/api/jobs", json={"after_prepare": prep, "models": MODELS[:1]}).get_json()["code"] == "prepare_failed"
    assert c.post("/api/jobs", json={"after_prepare": app["j1"], "models": MODELS[:1]}).status_code == 400


def test_resuming_a_chained_session_retries_its_data_preparation(tmp_path):
    import threading
    calls = {"prep": 0}
    queued = threading.Event()           # fail only once the benchmark is queued behind it

    def flaky_prepare(job, report):
        calls["prep"] += 1
        if calls["prep"] == 1:
            queued.wait(10)
            raise RuntimeError("disk full")
        return "csv_runs/x"
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results", gpu_available=lambda: False,
                     prepare_runner=flaky_prepare, runner=lambda job: str(tmp_path / "r.json"))
    up = tmp_path / "f.csv"
    up.write_text("x")
    prep = mgr.create_prepare_job({"kind": "upload", "path": str(up), "filename": "f.csv"}, name="f",
                                  max_flows=5, other_attack=False, limits={})["job_id"]
    bench = mgr.create_job(None, MODELS[:1], after_prepare=prep)["job_id"]
    queued.set()
    job = mgr.wait(bench, timeout=30)
    assert job["status"] == "failed" and "data preparation failed" in job["error"]
    mgr.validate = lambda run_dir, models, provider, base: {"n_flows": 5, "evaluation_mode": "efficiency_only",
                                                           "class_scheme": None}
    mgr.resolve_run = lambda run: tmp_path / "results" / "csv_runs" / "x"
    mgr.resume(bench)
    assert mgr.wait(bench, timeout=30)["status"] == "done" and calls["prep"] == 2
    assert mgr.get(prep)["status"] == "done"


def test_sessions_list_and_filters(app):
    c = app["c"]
    rows = c.get("/api/sessions").get_json()["sessions"]
    ids = [r["session_id"] for r in rows]
    mine = {app["j1"], app["j2"], app["j3"]}
    assert mine <= set(ids) and all(r["status"] == "Demo" for r in rows if r["session_id"] in mine)
    s1 = next(r for r in rows if r["session_id"] == app["j1"])
    assert s1["input_type"] == "CSV" and s1["n_flows"] == 10 and s1["gpu"] == "Demo (no GPU)"
    assert set(s1["metrics"]) == set(MODELS) and s1["more_efficient"]
    assert [r["session_id"] for r in c.get("/api/sessions?input=PCAP").get_json()["sessions"]] == [app["j2"]]
    assert c.get("/api/sessions?status=failed").get_json()["sessions"] == [] or \
        all(r["status"] == "Failed" for r in c.get("/api/sessions?status=failed").get_json()["sessions"])
    one = c.get(f"/api/sessions/{app['j1']}").get_json()
    assert one["identity"]["prepared_set_sha256"]


def test_compare_api(app):
    c = app["c"]
    like = c.get(f"/api/compare?a={app['j1']}&b={app['j3']}").get_json()
    assert like["validity"]["like_for_like"] is True
    unlike = c.get(f"/api/compare?a={app['j1']}&b={app['j2']}").get_json()
    assert unlike["validity"]["like_for_like"] is False and "Same prepared set (flows hash)" in unlike["validity"]["differences"]
    assert c.get(f"/api/compare?a={app['j1']}&b={app['j1']}").status_code == 400
    assert c.get(f"/api/compare?a={app['j1']}&b={app['p1']}").status_code == 400      # a prepare job is not a session


def test_leaderboard_api(app):
    lb = app["c"].get("/api/leaderboard?sort=latency").get_json()
    csv = next(g for g in lb["groups"] if g["input_type"] == "CSV")
    assert csv["n_sessions"] == 2 and [r["rank"] for r in csv["rows"]] == [1, 2]
    assert all(r["n_sessions"] == 2 and r["n_flows"] == 20 for r in csv["rows"])
    assert app["c"].get("/api/leaderboard?sort=nope").status_code == 400


def test_constraints_api_and_pdf(app):
    c, jid = app["c"], app["j1"]
    d = c.get(f"/api/jobs/{jid}/constraints?max_mean_latency_s=1000&max_peak_vram_mb=8000").get_json()
    assert d["limits_set"] and all(m["result"] == "no_data" for m in d["per_model"])
    assert c.get(f"/api/jobs/{jid}/constraints?max_mean_latency_s=abc").status_code == 400
    pypdf = pytest.importorskip("pypdf")
    pdf = c.get(f"/api/jobs/{jid}/report.pdf?max_mean_latency_s=1000")
    assert pdf.status_code == 200
    text = "\n".join(pg.extract_text() for pg in pypdf.PdfReader(io.BytesIO(pdf.data)).pages)
    for needle in ("Agents", "per-agent breakdown", "agentic overhead", "Cost and energy", "not measured",
                   "Decision helper", "Max mean latency 1000", "Statistical depth", "Cliff's delta",
                   "Sample too small"):
        assert needle in text, needle
    plain = "\n".join(pg.extract_text() for pg in pypdf.PdfReader(io.BytesIO(c.get(f"/api/jobs/{jid}/report.pdf").data)).pages)
    assert "No limits were set" in plain
    assert c.get(f"/api/jobs/{jid}/report.pdf?max_mean_latency_s=-3").status_code == 400


def test_listings_and_stored_results_need_a_read_token(app, monkeypatch):
    """Phase 47 (replaces the passcode): with a control plane, listings, a session by id,
    compare, results and the decision helper need a run token of kind read."""
    import secrets as _s
    import time as _t

    from agentmeter.server import runtoken
    secret = "test-run-token-secret-0123456789"
    monkeypatch.setenv("AGENTMETER_RUN_TOKEN_SECRET", secret)
    now = int(_t.time())
    tok = runtoken.sign({"typ": "run", "aud": "agentmeter-backend", "jti": _s.token_hex(16), "sub": "u1",
                         "uname": "alice", "kind": "read", "iat": now, "exp": now + 300}, secret)
    c = app["srv"].create_app(local_results_dir=str(app["tmp"] / "results"), job_manager=app["mgr"]).test_client()
    for path in ("/api/sessions", "/api/leaderboard", "/api/jobs", f"/api/sessions/{app['j1']}",
                 f"/api/compare?a={app['j1']}&b={app['j3']}",
                 f"/api/jobs/{app['j1']}/constraints?max_mean_latency_s=1", f"/api/jobs/{app['j1']}/result"):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers={"Authorization": f"Bearer {tok}"}).status_code == 200, path


def test_app_routes(app):
    c = app["c"]
    assert c.get("/").status_code == 200 and b"New benchmark" in c.get("/").data
    r = c.get(f"/?session={app['j1']}")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/#/session/{app['j1']}/detailed")
    assert c.get("/?session=../../etc").status_code == 200                # not a job id: just the app
    for path in ("/baseline", "/analysis"):
        page = c.get(path).get_data(as_text=True)
        assert "embed" in page and "agentmeter-embed-height" in page


def test_ops_settings(monkeypatch):
    from agentmeter.session.benchmark import ops_settings
    monkeypatch.delenv("AGENTMETER_GPU_USD_PER_HOUR", raising=False)
    monkeypatch.delenv("AGENTMETER_POWER_SAMPLE_S", raising=False)
    assert ops_settings() == {"gpu_usd_per_hour": None, "sample_interval_s": 0.5, "idle_sample_s": 5.0}
    monkeypatch.setenv("AGENTMETER_GPU_USD_PER_HOUR", "0.79")
    monkeypatch.setenv("AGENTMETER_POWER_SAMPLE_S", "0.01")
    assert ops_settings()["gpu_usd_per_hour"] == 0.79 and ops_settings()["sample_interval_s"] == 0.05
