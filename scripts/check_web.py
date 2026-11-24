"""Syntax-check every script the website ships: web/**/*.js and the inline <script> blocks of
web/*.html, with `node --check`. One broken quote takes a whole page down (24 November 2026), so
this runs first in scripts/test_all.sh and as tests/test_web_syntax.py.

  python scripts/check_web.py        # exit 1 and the error when a script does not parse
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def problems() -> list[str]:
    node = shutil.which("node")
    if not node:
        return ["node is not installed: the web scripts were not checked"]
    out = []
    with tempfile.TemporaryDirectory() as td:
        files = sorted(WEB.rglob("*.js"))
        for html in sorted(WEB.glob("*.html")):
            for i, block in enumerate(re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/(?:ld\+)?json\")[^>]*>(.*?)</script>",
                                                 html.read_text(encoding="utf-8"), re.S)):
                if block.strip():
                    p = Path(td) / f"{html.stem}.inline{i}.js"
                    p.write_text(block, encoding="utf-8")
                    files.append(p)
        for f in files:
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            if r.returncode:
                name = f.relative_to(WEB.parent) if WEB in f.parents else f.name
                out.append(f"{name}: " + "\n".join(r.stderr.strip().splitlines()[:5]))
    out += _csp_problem()
    return out


def _csp_problem() -> list[str]:
    """web/_headers allows inline scripts by their SHA-256: an edited inline script is blocked in the
    browser until the headers are regenerated."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("csp_headers", WEB.parent / "scripts" / "csp_headers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if (WEB / "_headers").read_text(encoding="utf-8") != m.headers():
        return ["web/_headers is out of date with the inline scripts: run  python scripts/csp_headers.py"]
    return []


if __name__ == "__main__":
    bad = problems()
    for b in bad:
        print(b)
    sys.exit(1 if bad and not bad[0].startswith("node is not installed") else 0)
