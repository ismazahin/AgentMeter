"""Experiments: model size (precision) x number of agents, one upload, one job per combination."""
from __future__ import annotations

import copy

import pytest

from agentmeter.config import load_config
from agentmeter.pipeline.agents import LADDERS, ROLE_KIND, handoffs, ladder
from agentmeter.pipeline.pipeline import Pipeline
from agentmeter.pipeline.spec import describe, label_agent, verdict_agent
from agentmeter.providers.mock import MockProvider
from agentmeter.session import experiment as X

from test_service_api import SAMPLE_CSV, env, upload  # noqa: F401  (the Flask fixture)

FLOW = ("- Destination Port: 22\n- Flow Duration: 500000\n- Total Fwd Packets: 80\n- Total Bwd Packets: 70\n"
        "- Flow Packets/s: 300\n- SYN Flag Count: 1")


@pytest.mark.parametrize("n", sorted(LADDERS))
def test_every_pipeline_size_ends_with_a_label_and_a_verdict(n):
    cfg = copy.deepcopy(load_config())
    cfg.data["pipeline"]["agents"] = ladder(n)
    out = Pipeline(cfg, MockProvider(cfg)).run("s1", FLOW)
    assert out["predicted_class"] == "Brute Force" and out["verdict"]["mitre_technique"].startswith("T1110")
    assert all(out.get(a) for a in ladder(n)) and len(ladder(n)) == n
    pl = {"agents": ladder(n), "prompt_version": "triage-2"}
    assert label_agent(pl) and verdict_agent(pl)


def test_every_agent_reads_all_earlier_outputs():
    cfg = copy.deepcopy(load_config())
    cfg.data["pipeline"]["agents"] = ladder(6)
    seen = []

    class Spy(MockProvider):
        def generate(self, prompt, system=None, max_new_tokens=None):
            seen.append(prompt)
            return super().generate(prompt, system=system, max_new_tokens=max_new_tokens)
    Pipeline(cfg, Spy(cfg)).run("s1", FLOW)
    assert "Earlier agents" not in seen[0]
    last = seen[-1]
    assert all(f"[{a.capitalize()}]" in last for a in ladder(6)[:-1])
    assert handoffs(ladder(4))["act"] == ["perceive", "reason", "decide"]
    assert describe({"agents": ladder(4), "prompt_version": "triage-2"})[3]["reads"] == ["perceive", "reason", "decide"]
    assert describe({"agents": ladder(4), "prompt_version": "triage-1"})[3]["reads"] == ["reason"]
    assert set(ROLE_KIND) >= {a for v in LADDERS.values() for a in v}


def test_plan_validates_caps_and_orders_the_runs():
    p = X.plan(["a", "b"], ["8bit", "4-bit"], [10, 2, 4])
    assert p["precisions"] == ["4bit", "8bit"] and p["agents"] == [2, 4, 10] and len(p["cells"]) == 12
    assert [(j["precision"], j["agents"]) for j in p["jobs"]][:3] == [("4bit", 2), ("4bit", 4), ("4bit", 10)]
    assert X.plan(["a"], None, None)["is_experiment"] is False
    with pytest.raises(X.ExperimentError):
        X.plan(["a", "b", "c"], ["4bit", "8bit", "16bit"], [2, 4])           # 18 runs
    with pytest.raises(X.ExperimentError):
        X.plan(["a"], ["2bit"], [4])
    with pytest.raises(X.ExperimentError):
        X.plan(["a"], ["4bit"], [5])
    assert X.vram_need_gb(7.6, "16bit") > 16 > X.vram_need_gb(7.6, "4bit")


def test_a_cell_sets_quantization_and_pipeline_so_cells_never_pool():
    from agentmeter.session.analyses import settings_fingerprint, settings_of
    base = copy.deepcopy(load_config().data)
    a = X.apply_variant(copy.deepcopy(base), "4bit", 4)
    b = X.apply_variant(copy.deepcopy(base), "8bit", 4)
    c = X.apply_variant(copy.deepcopy(base), "4bit", 10)
    assert b["model"]["hf"]["quantization"]["load_in_8bit"] and c["pipeline"]["agents"] == ladder(10)
    fps = {settings_fingerprint(settings_of(x)) for x in (a, b, c)}
    assert len(fps) == 3


def test_one_request_runs_the_whole_grid_and_stop_ends_it(env):  # noqa: F811
    c, mgr, _ = env
    run = upload(c, SAMPLE_CSV, max_flows="4").get_json()["run"]
    r = c.post("/api/jobs", json={"run": run, "models": ["a"], "provider": "mock",
                                  "precisions": ["4bit", "8bit"], "agents": [1, 4]})
    assert r.status_code == 202, r.get_json()
    ids = r.get_json()["experiment_jobs"]
    assert len(ids) == 4
    jobs = [mgr.get(i) for i in ids]
    assert [(j["precision"], j["agents"]) for j in jobs] == [("4bit", 1), ("4bit", 4), ("8bit", 1), ("8bit", 4)]
    exp = jobs[0]["experiment"]["id"]
    for i in ids:
        mgr.wait(i, timeout=180)
    res = c.get(f"/api/jobs/{ids[0]}/result").get_json()
    assert res["session"]["variant"]["agents"] == 1 and res["session"]["experiment"]["id"] == exp
    assert res["agents"]["agent_order"] == ["triage"]
    got = c.get(f"/api/experiments/{exp}").get_json()
    assert [j["job_id"] for j in got["jobs"]] == ids and all(j["status"] == "done" for j in got["jobs"])
    # stopping one run of an experiment cancels the runs still waiting
    r = c.post("/api/jobs", json={"run": upload(c, SAMPLE_CSV, max_flows="3").get_json()["run"], "models": ["a"],
                                  "provider": "mock", "precisions": ["4bit"], "agents": [1, 2, 3]})
    ids = r.get_json()["experiment_jobs"]
    c.post(f"/api/jobs/{ids[0]}/stop")
    for i in ids:
        mgr.wait(i, timeout=60)
    assert {mgr.get(i)["status"] for i in ids[1:]} == {"stopped"}


def test_too_many_runs_and_bad_sizes_are_refused(env):  # noqa: F811
    c, _, _ = env
    run = upload(c, SAMPLE_CSV, max_flows="3").get_json()["run"]
    r = c.post("/api/jobs", json={"run": run, "models": ["a", "b"], "provider": "mock",
                                  "precisions": ["4bit", "8bit", "16bit"], "agents": [1, 4, 10]})
    assert r.status_code == 400 and r.get_json()["code"] == "too_many_runs"
    r = c.post("/api/jobs", json={"run": run, "models": ["a"], "provider": "mock", "agents": [7]})
    assert r.status_code == 400 and r.get_json()["code"] == "bad_experiment"
