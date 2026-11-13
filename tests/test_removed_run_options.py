"""Load scaling, the repeat check and user-defined pipelines were removed (2026-11-13).

Sessions recorded before still open: their stored pipeline section is read for display, but
no new run can use a custom agent.
"""
import pytest

from agentmeter.pipeline.spec import describe, label_agent

OLD_CUSTOM = {"agents": ["perceive", "triage"],
              "custom": {"triage": {"title": "Triage", "kind": "label", "reads": ["perceive"],
                                    "features": True, "instructions": "Pick the attack class."}}}


def test_an_old_custom_pipeline_config_is_still_described():
    d = describe(OLD_CUSTOM)
    assert [a["name"] for a in d] == ["perceive", "triage"]
    assert d[1]["builtin"] is False and d[1]["kind"] == "label" and d[1]["reads"] == ["perceive"]
    assert label_agent(OLD_CUSTOM) == "triage"
    assert label_agent(None) == "decide"


def test_a_custom_agent_can_no_longer_run():
    from agentmeter.config import Config
    from agentmeter.pipeline import Pipeline
    cfg = Config({"pipeline": OLD_CUSTOM, "classes": ["Benign"]}, path=None)
    with pytest.raises(ValueError, match="Unknown agent"):
        Pipeline(cfg, provider=None)


def test_the_removed_modules_are_gone():
    import importlib
    with pytest.raises(ImportError):
        importlib.import_module("agentmeter.run.load_test")
    from agentmeter.run import extras
    assert not hasattr(extras, "run_repeat_check") and "repeat_check" not in extras.KINDS
