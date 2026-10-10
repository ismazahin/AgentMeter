"""Export configs/constraint_rules.yaml to worker/src/constraint_rules.json (the control plane
bundles it; tests/test_control_plane.py fails if the two drift apart).

    python scripts/export_constraint_rules.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentmeter.session.constraints import load_rules  # noqa: E402

OUT = REPO / "worker" / "src" / "constraint_rules.json"


def exported() -> str:
    rb = load_rules()
    return json.dumps({"source": rb["source"], "version": rb["version"], "inputs": rb["inputs"],
                       "rules": rb["rules"]}, indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(exported(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}")
