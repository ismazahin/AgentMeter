"""Build web/prep/agentmeter-prep.zip: the data-preparation code that runs IN THE BROWSER.

"Prepare on this computer" (New benchmark) runs agentmeter.server.prepare.prepare_file in the
user's browser with Pyodide (Python compiled to WebAssembly, in a Web Worker: web/prep/
prep-worker.js), so the laptop's CPU does the sampling while the GPU server keeps benchmarking.
It is the SAME Python code as the server's, so a set prepared in the browser is byte-identical
in its data files (features.csv, labels.csv, selected flows) to one prepared on the server.

This zip holds only what preparation imports, plus the configs it reads, with an empty
top-level agentmeter/__init__.py (the real one imports the database and model layers, which the
browser neither needs nor can load). Deterministic (sorted names, fixed timestamps), so a test
can check it is current:

    python scripts/build_web_prep.py          # after changing anything listed in FILES
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "web" / "prep" / "agentmeter-prep.zip"
FILES = (["agentmeter/config.py", "agentmeter/server/__init__.py", "agentmeter/server/prepare.py",
          "agentmeter/session/__init__.py", "agentmeter/session/prepared_hash.py",
          "agentmeter/data/__init__.py", "agentmeter/data/dataprep.py",
          "config.yaml", "pyproject.toml", "configs/flow_rules.yaml", "configs/input_formats.yaml"]
         + sorted(str(p.relative_to(REPO)) for p in (REPO / "agentmeter" / "ingest").glob("*.py")))
STUB_INIT = '"""AgentMeter (browser data-preparation bundle: scripts/build_web_prep.py)."""\n'


def build() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        entries = {"agentmeter/__init__.py": STUB_INIT.encode()}
        for f in FILES:
            entries[f] = (REPO / f).read_bytes()
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, entries[name])
    return buf.getvalue()


def main() -> int:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists() and OUT.read_bytes() == data:
        print(f"{OUT.relative_to(REPO)} is up to date")
        return 0
    OUT.write_bytes(data)
    print(f"wrote {OUT.relative_to(REPO)} ({len(data):,} bytes, {len(FILES) + 1} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
