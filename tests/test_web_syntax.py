"""Every script the website ships parses (scripts/check_web.py)."""
import importlib.util
import shutil
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_every_web_script_parses():
    spec = importlib.util.spec_from_file_location("check_web", Path(__file__).resolve().parents[1] / "scripts" / "check_web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.problems() == []
