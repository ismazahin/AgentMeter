"""User-defined pipelines (pipeline/spec.py): validation, the custom agent, and a real
mock session that runs one end to end through the runner and the analyses."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT, Config, load_config
from agentmeter.ingest.run import process_csv
from agentmeter.pipeline import Pipeline
from agentmeter.pipeline.agents import HANDOFFS, custom_system_prompt
from agentmeter.pipeline.spec import SpecError, apply_to_config, describe, label_agent, normalize
from agentmeter.providers.base import GenerationResult
from agentmeter.providers.mock import MockProvider
from agentmeter.session.benchmark import SessionError, prepare_session, run_session

SAMPLE = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
VERIFY = {"name": "verify", "kind": "text", "instructions": "Check the reasoning for mistakes and say so.",
          "reads": ["reason"]}
TRIAGE = {"name": "triage", "title": "Triage", "kind": "label", "instructions": "Pick the attack class.",
          "reads": ["verify", "perceive"], "features": False}
CUSTOM = ["perceive", "reason", VERIFY, TRIAGE]


# ---------------------------------------------------------------- validation
def test_the_default_four_and_none_mean_the_base_pipeline():
    assert normalize(None) is None
    assert normalize(["perceive", "reason", "decide", "act"]) is None
    assert normalize([{"name": n} for n in ["perceive", "reason", "decide", "act"]]) is None


def test_a_custom_pipeline_is_normalized_with_defaults():
    spec = normalize(CUSTOM)
    assert [a["name"] for a in spec] == ["perceive", "reason", "verify", "triage"]
    assert spec[0] == {"name": "perceive", "builtin": True, "kind": "text"}
    assert spec[2] == {"name": "verify", "title": "Verify", "kind": "text",
                       "instructions": "Check the reasoning for mistakes and say so.",
                       "reads": ["reason"], "features": True, "max_new_tokens": 192}
    assert spec[3]["max_new_tokens"] == 16 and spec[3]["features"] is False


@pytest.mark.parametrize("spec,msg", [
    ([], "list of 1 to 8"),
    ("perceive", "list of 1 to 8"),
    (["perceive"] * 9, "at most 8"),
    (["perceive", "reason"], "own label agent): it has 0"),
    (["decide", {**TRIAGE, "reads": [], "features": True}], "own label agent): it has 2"),
    (["perceive", "act", "decide"], "verdict agent must come after"),
    (["decide", "act", {"name": "act2", "kind": "verdict", "instructions": "Give the final verdict."}], "at most one verdict"),
    (["decide", "decide"], "appears twice"),
    (["decide", {"name": "Bad Name", "instructions": "xxxxxxxxxxxx"}], "lowercase letters"),
    (["decide", {"name": "verdict", "instructions": "xxxxxxxxxxxx"}], "lowercase letters"),
    (["decide", {"name": "reason", "instructions": "my own reasoner"}], "built-in agent"),
    (["decide", {"name": "x1", "instructions": "short"}], "instructions (10 to 1500"),
    (["decide", {"name": "x1", "instructions": "a" * 1501}], "instructions (10 to 1500"),
    (["decide", {"name": "x1", "kind": "summary", "instructions": "summarise it"}], "kind must be"),
    (["decide", {"name": "x1", "instructions": "summarise it", "reads": ["x2"]}], "only read agents that run before"),
    ([{"name": "x1", "instructions": "summarise it", "reads": ["decide"]}, "decide"], "only read agents that run before"),
    (["decide", {"name": "x1", "instructions": "summarise it", "features": False}], "has no input"),
    (["decide", {"name": "x1", "instructions": "summarise it", "max_new_tokens": 4}], "8 to 1024"),
    (["decide", {"name": "x1", "instructions": "summarise it", "max_new_tokens": True}], "8 to 1024"),
    (["decide", {"name": "x1", "instructions": "summarise it", "title": "t" * 41}], "title"),
])
def test_invalid_pipelines_are_refused_with_a_reason(spec, msg):
    with pytest.raises(SpecError, match=re.escape(msg)):
        normalize(spec)


def test_normalize_is_idempotent():
    spec = normalize(CUSTOM)
    assert normalize(spec) == spec
    with pytest.raises(SpecError, match="built-in agent"):
        normalize(["perceive", {"name": "decide", "kind": "text"}])


def test_apply_and_describe_round_trip():
    data = copy.deepcopy(load_config().data)
    apply_to_config(normalize(CUSTOM), data)
    pl = data["pipeline"]
    assert pl["agents"] == ["perceive", "reason", "verify", "triage"]
    assert set(pl["custom"]) == {"verify", "triage"}
    assert pl["max_new_tokens"] == {"perceive": load_config().get("pipeline.max_new_tokens.perceive"),
                                    "reason": load_config().get("pipeline.max_new_tokens.reason"),
                                    "verify": 192, "triage": 16}
    d = describe(pl)
    assert [(a["name"], a["kind"], a["reads"]) for a in d] == [
        ("perceive", "text", []), ("reason", "text", ["perceive"]),
        ("verify", "text", ["reason"]), ("triage", "label", ["verify", "perceive"])]
    assert label_agent(pl) == "triage"
    # the default chain describes exactly the built-in hand-offs
    assert {a["name"]: a["reads"] for a in describe(load_config().get("pipeline"))} == HANDOFFS


# ---------------------------------------------------------------- the custom agent
class Recorder(MockProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = []

    def generate(self, prompt, system=None, max_new_tokens=None):
        self.calls.append((system, prompt, max_new_tokens))
        return super().generate(prompt, system=system, max_new_tokens=max_new_tokens)


def _cfg(spec):
    base = load_config(PROJECT_ROOT / "configs" / "run_full_mock.yaml")
    data = copy.deepcopy(base.data)
    apply_to_config(normalize(spec), data)
    return Config(data, base.path)


FLOW = "- Destination Port: 22\n- Total Fwd Packets: 80\n- Flow Duration: 9000000"


def test_custom_agents_get_their_inputs_and_the_label_ends_the_pipeline():
    cfg = _cfg(CUSTOM)
    prov = Recorder(cfg)
    out = Pipeline(cfg, prov).run("s1", FLOW)
    sys_v, user_v, tok_v = prov.calls[2]
    assert sys_v == custom_system_prompt(cfg.get("pipeline.custom.verify"))
    assert sys_v.startswith("You are the Verify agent") and "Check the reasoning" in sys_v
    assert "Reason output:\n" + out["reason"] in user_v and "Raw features:\n" + FLOW in user_v and tok_v == 192
    sys_t, user_t, tok_t = prov.calls[3]
    assert "Allowed labels (copy one verbatim)" in user_t and "Raw features" not in user_t
    assert user_t.index("Verify output") < user_t.index("Perceive output") and tok_t == 16
    assert out["verify"] == "Verify note: the flow is consistent with Brute Force."
    # (the mock classifies from the features in its own prompt; Triage gets none, so: Benign)
    assert out["triage"] == "Classification: Benign" and out["predicted_class"] == "Benign"
    # no verdict agent: the verdict is the label (+ its MITRE technique from the config)
    assert out["verdict"]["predicted_class"] == "Benign"
    assert out["verdict"]["mitre_technique"] == cfg.get("mitre")["Benign"]


def test_a_custom_verdict_agent_writes_the_verdict():
    spec = ["decide", {"name": "report", "kind": "verdict", "instructions": "Write the final verdict for the analyst."}]
    cfg = _cfg(spec)
    prov = Recorder(cfg)
    out = Pipeline(cfg, prov).run("s1", FLOW)
    assert "Chosen class: Brute Force" in prov.calls[1][1]
    assert out["verdict"]["predicted_class"] == "Brute Force" and "MITRE ATT&CK" in out["verdict"]["raw"]


def test_a_label_agent_with_an_unusable_reply_is_unparseable():
    cfg = _cfg(["perceive", {**TRIAGE, "reads": ["perceive"]}])

    class Junk(MockProvider):
        def generate(self, prompt, system=None, max_new_tokens=None):
            return GenerationResult(text="no idea", input_tokens=3, output_tokens=2, ttft_s=None)
    out = Pipeline(cfg, Junk(cfg)).run("s1", FLOW)
    assert out["predicted_class"] == "Unparseable" and out["verdict"]["predicted_class"] == "Unparseable"


def test_the_default_pipeline_prompts_are_unchanged():
    """Built-ins keep their own prompts: a default session measures what it always did."""
    cfg = load_config(PROJECT_ROOT / "configs" / "run_full_mock.yaml")
    prov = Recorder(cfg)
    out = Pipeline(cfg, prov).run("s1", FLOW)
    assert [c[0].split(".")[0] for c in prov.calls] == [
        "You are the Perceive agent in a network threat-detection pipeline", "You are the Reason agent",
        "You are the Decide agent", "You are the Act agent"]
    assert out["decide"] == "Classification: Brute Force" and out["act"].startswith("Predicted class:")
    assert out["verdict"]["predicted_class"] == "Brute Force"     # Act keeps the class Decide chose


# ---------------------------------------------------------------- a real session
@pytest.fixture(scope="module")
def session(tmp_path_factory):
    root = tmp_path_factory.mktemp("csv_runs")
    process_csv(SAMPLE, out_root=root, max_flows=8)
    run_dir = root / "cicids2017_sample"
    payload = run_session(run_dir, ["mock-a"], provider="mock", pipeline=CUSTOM)
    return run_dir, payload


def test_prepare_session_writes_the_pipeline_and_refuses_a_bad_one(session, tmp_path):
    run_dir, _ = session
    cfg = yaml.safe_load((run_dir / "session_config.yaml").read_text())
    assert cfg["pipeline"]["agents"] == ["perceive", "reason", "verify", "triage"]
    with pytest.raises(SessionError, match="exactly one label agent"):
        prepare_session(run_dir, ["mock-a"], provider="mock", pipeline=["perceive"])


def test_the_session_measures_every_custom_agent(session):
    run_dir, p = session
    ag = p["agents"]
    assert ag["agent_order"] == ["perceive", "reason", "verify", "triage"]
    assert ag["label_agent"] == "triage"
    assert ag["handoffs"] == {"perceive": [], "reason": ["perceive"], "verify": ["reason"], "triage": ["verify", "perceive"]}
    assert [a["name"] for a in ag["pipeline"]] == ag["agent_order"] and ag["pipeline"][2]["instructions"].startswith("Check")
    rows = {a["agent"]: a for a in ag["per_model"][0]["agents"]}
    assert set(rows) == set(ag["agent_order"]) and all(r["n"] == 8 for r in rows.values())
    assert rows["triage"]["failures"]["unparseable_label"] == 0 and rows["verify"]["failures"]["unparseable_label"] is None
    assert rows["verify"]["failures"]["token_cap"] == 192
    ov = ag["per_model"][0]["overhead"]
    assert ov["handoff_tokens_per_flow"] > 0
    assert p["session"]["accuracy_available"] is True and p["per_model"][0]["accuracy"]["accuracy"] is not None
    # the flow explorer has every custom agent's text
    flows = json.loads(next(run_dir.glob("flows_*.json")).read_text())
    first = flows["flows"][0] if isinstance(flows, dict) else flows[0]
    assert "verify" in json.dumps(first) and "Verify note" in json.dumps(first)


def test_a_custom_pipeline_is_not_like_for_like_with_the_default(session, tmp_path):
    _, p = session
    from agentmeter.session.analyses import settings_fingerprint, settings_of
    st = p["session_identity"]["settings"]
    assert st["pipeline.agents"] == ["perceive", "reason", "verify", "triage"] and "verify" in st["pipeline.custom"]
    default = settings_of(load_config(PROJECT_ROOT / "configs" / "run_full_mock.yaml").data)
    assert "pipeline.custom" not in default          # default sessions keep their old fingerprint
    assert settings_fingerprint(default) != p["session_identity"]["settings_fingerprint"]
