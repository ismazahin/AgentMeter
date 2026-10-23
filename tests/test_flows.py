"""Flow explorer data: agent answers stored with each flow (agent_outputs, same transaction),
the per-run flows_<run>.json, its endpoint and its control-plane upload."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.db.storage import Storage
from agentmeter.ingest.run import process_csv
from agentmeter.pipeline.instrument import AgentMetrics
from agentmeter.server.jobs import JobManager
from agentmeter.session.analyses import FLOW_TEXT_CHARS, flows_file


def _row(agent, out=5):
    return AgentMetrics(agent_name=agent, model="m", scenario_id="s1", wall_time_s=0.1, ttft_s=None,
                        vram_peak_mb=None, input_tokens=10, output_tokens=out)


def test_answers_are_stored_with_the_flow_and_replaced_on_rerun(tmp_path):
    db = tmp_path / "s.db"
    st = Storage(db)
    st.create_run("r1", "fp", "4bit", "cpu")
    for text in ("first answer", "second answer"):           # a re-run of the flow replaces, never duplicates
        st.persist_scenario("r1", "m", "s1", "Benign", "Benign", True, 0.4, None,
                            [_row("perceive"), _row("decide", 0)], outputs={"perceive": text, "decide": None})
    st.close()
    con = sqlite3.connect(db)
    assert con.execute("SELECT agent_name, text FROM agent_outputs ORDER BY agent_name").fetchall() == [
        ("decide", None), ("perceive", "second answer")]
    con.close()


def test_flows_file_joins_answers_times_and_tokens(tmp_path):
    db = tmp_path / "s.db"
    st = Storage(db)
    st.create_run("r1", "fp", "4bit", "cpu")
    long = "x" * (FLOW_TEXT_CHARS + 50)
    st.persist_scenario("r1", "mock:a", "s1", "DoS", "Benign", False, 0.4, None,
                        [AgentMetrics(scenario_id="s1", model="mock:a", agent_name="perceive", wall_time_s=0.25, ttft_s=None,
                                      vram_peak_mb=None, input_tokens=10, output_tokens=5),
                         AgentMetrics(scenario_id="s1", model="mock:a", agent_name="decide", wall_time_s=0.15, ttft_s=None,
                                      vram_peak_mb=None, input_tokens=20, output_tokens=1)],
                        outputs={"perceive": long, "decide": "Classification: DoS"})
    st.close()
    f = flows_file(db, "r1", {"a": "mock:a"})
    assert f["has_text"] and f["models"] == ["a"] and f["agent_order"][0] == "perceive"
    flow = f["flows"][0]
    assert flow["scenario_id"] == "s1" and flow["truth"] == "Benign"
    pm = flow["per_model"]["a"]
    assert pm["predicted"] == "DoS" and pm["correct"] is False and pm["latency_s"] == 0.4
    assert [a["agent"] for a in pm["agents"]] == ["perceive", "decide"]
    assert pm["agents"][0]["text"].endswith("…[cut]") and len(pm["agents"][0]["text"]) == FLOW_TEXT_CHARS + len(" …[cut]")
    assert pm["agents"][1] == {"agent": "decide", "wall_s": 0.15, "input_tokens": 20, "output_tokens": 1, "text": "Classification: DoS"}
    un = flows_file(db, "r1", {"a": "mock:a"}, labelled=False)        # PCAP: no ground truth to compare with
    assert un["labelled"] is False and un["flows"][0]["truth"] is None and un["flows"][0]["per_model"]["a"]["correct"] is None


def test_a_session_writes_flows_and_serves_them(tmp_path):
    pytest.importorskip("flask")
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path / "results" / "csv_runs", max_flows=6)
    mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results")
    j = mgr.create_job("csv_runs/cicids2017_sample", ["mock-a", "mock-b"], provider="mock")
    assert mgr.wait(j["job_id"])["status"] == "done"
    from agentmeter.server.jobs_api import flows_path
    p = flows_path(mgr.get(j["job_id"]), mgr.result(j["job_id"]))
    data = json.loads(p.read_text())
    assert data["has_text"] and len(data["flows"]) == 6 and data["models"] == ["mock-a", "mock-b"]
    assert all(a["text"] for f in data["flows"] for pm in f["per_model"].values() for a in pm["agents"])
    # the control-plane upload carries it as flows.json
    from agentmeter.server.control_plane import session_upload
    _, files, _ = session_upload(mgr, mgr.get(j["job_id"]))
    assert json.loads(files["flows.json"]) == data
