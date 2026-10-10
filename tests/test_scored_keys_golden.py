"""Phase 46: existing scored keys are byte-identical before / after the phase.

tests/fixtures/golden_session/ holds a mock session (10 labelled flows x 2 models):
its session.db plus `golden_session_results.json`, written by the code BEFORE Phase 46
(commit 28f5f4d). Re-scoring the same DB with the current code must give exactly the
same JSON for every score, rank, SAW value, statistic and verdict. New Phase-46
analyses may only ADD keys.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentmeter.session.benchmark import prepare_session
from agentmeter.session.scoring import score_session

FIXTURE = Path(__file__).parent / "fixtures" / "golden_session"
MODELS = ["Qwen/Qwen2.5-7B-Instruct", "microsoft/Phi-3-mini-4k-instruct"]
SCORED_KEYS = ["per_model", "comparison", "relative_comparison", "phase8", "phase8_efficiency_only",
               "sensitivity", "statistics", "phase7", "per_agent", "per_class", "advanced", "notes"]
PATH_KEYS = ("run_dir", "db_path")          # where the copy lives — not a score


def _dump(v) -> str:
    return json.dumps(v, sort_keys=True, allow_nan=False)


def _rescore(tmp_path: Path) -> dict:
    run = tmp_path / "golden"
    shutil.copytree(FIXTURE, run)
    golden = json.loads((run / "golden_session_results.json").read_text())
    plan = prepare_session(run, MODELS, provider="mock")
    return golden, json.loads(json.dumps(score_session(plan, golden["session"]["run_id"]), default=str))


def test_scored_keys_are_byte_identical_to_the_pre_phase46_output(tmp_path):
    golden, now = _rescore(tmp_path)
    for key in SCORED_KEYS:
        assert key in golden, key
        assert _dump(now[key]) == _dump(golden[key]), f"{key} changed"
    strip = lambda s: {k: v for k, v in s.items() if k not in PATH_KEYS}   # noqa: E731
    assert _dump(strip(now["session"])) == _dump(strip(golden["session"]))


def test_new_keys_are_only_additions(tmp_path):
    golden, now = _rescore(tmp_path)
    missing = set(golden) - set(now) - {"environment", "model_context", "recommendation_stage3"}
    assert not missing, missing        # (environment + stage 3 are added by run_session, not scoring)
