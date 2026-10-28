"""Prepare on this computer (web/prep): the browser bundle is current, self-sufficient and gives
the same data as the server; the backend serves the worker's files; a browser-prepared set is
imported (hash-checked) under its own name."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.server import prepare as P

spec = importlib.util.spec_from_file_location("build_web_prep", PROJECT_ROOT / "scripts" / "build_web_prep.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
LIMITS = dict(max_csv_rows=500_000, max_pcap_packets=100_000, max_pcap_flows=20_000, pcap_windows=10)


def test_the_bundle_is_current():
    assert B.OUT.read_bytes() == B.build(), "run: python scripts/build_web_prep.py"
    names = zipfile.ZipFile(B.OUT).namelist()
    assert "agentmeter/server/prepare.py" in names and "configs/flow_rules.yaml" in names
    assert not any(n.startswith(("agentmeter/db/", "agentmeter/run/", "agentmeter/providers/")) for n in names)


def test_the_bundle_alone_prepares_csv_and_pcap_like_the_server(tmp_path):
    """The zip on sys.path (as in the browser), sqlite3 blocked (Pyodide has none): same data files."""
    pytest.importorskip("scapy")
    app = tmp_path / "app"
    zipfile.ZipFile(B.OUT).extractall(app)
    code = f"""
import sys, json
sys.modules["sqlite3"] = None                      # the browser has no sqlite3: any import fails
sys.path.insert(0, {str(app)!r})
from pathlib import Path
from agentmeter.server import prepare as P
assert P.__file__.startswith({str(app)!r}), P.__file__
out = {{}}
for kind, src in (("csv", {str(SAMPLE_CSV)!r}), ("pcap", {str(SAMPLE_PCAP)!r})):
    sid = P.prepare_file(Path(src), source={{"filename": Path(src).name}}, results_root=Path({str(tmp_path / 'b')!r}),
                         name="b_" + kind, max_flows=20, other_attack=False, limits={LIMITS!r},
                         environment={{"provider": "browser"}})
    out[kind] = sid
print(json.dumps(out))
"""
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    sids = json.loads(r.stdout.strip().splitlines()[-1])
    for kind, src in (("csv", SAMPLE_CSV), ("pcap", SAMPLE_PCAP)):
        sid = P.prepare_file(src, source={"filename": src.name}, results_root=tmp_path / "s", name="s_" + kind,
                             max_flows=20, other_attack=False, limits=LIMITS)
        for f in ("features.csv", "labels.csv", "selected_flows.csv"):
            a, b = tmp_path / "b" / sids[kind] / f, tmp_path / "s" / sid / f
            assert a.exists() == b.exists() and (not a.exists() or a.read_bytes() == b.read_bytes()), (kind, f)
        man = json.loads((tmp_path / "b" / sids[kind] / "manifest.json").read_text())["prepared_set"]
        assert man["backend"]["provider"] == "browser"


def test_a_browser_prepared_set_is_imported_under_its_own_name(tmp_path):
    sid = P.prepare_file(SAMPLE_CSV, source={"filename": SAMPLE_CSV.name}, results_root=tmp_path / "b",
                         name=P.unique_name(SAMPLE_CSV.name), max_flows=10, other_attack=False, limits=LIMITS,
                         environment={"provider": "browser"})
    d = tmp_path / "b" / sid
    files = {n: (d / n).read_bytes() for n in ("manifest.json", "features.csv", "labels.csv")}
    got = P.import_prepared(files, tmp_path / "server")
    assert got == sid                                         # no "_import" suffix, no second timestamp
    assert P.import_prepared(files, tmp_path / "server") != sid      # taken: a fresh unique name
    bad = dict(files, **{"features.csv": files["features.csv"].replace(b"\n", b"\n", 1) + b"x"})
    with pytest.raises(P.PrepareError, match="sha256"):
        P.import_prepared(bad, tmp_path / "server")          # still hash-checked


def test_the_backend_serves_the_worker_files(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    for k in ("AGENTMETER_RUN_TOKEN_SECRET", "AGENTMETER_WORKER_URL", "AGENTMETER_CONTROL_PLANE_URL", "AGENTMETER_BACKEND_SECRET"):
        monkeypatch.delenv(k, raising=False)
    spec2 = importlib.util.spec_from_file_location("serve_prep", PROJECT_ROOT / "scripts" / "serve.py")
    srv = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(srv)
    from agentmeter.server.jobs import JobManager
    c = srv.create_app(local_results_dir=str(tmp_path), job_manager=JobManager(jobs_dir=tmp_path / "j", results_root=tmp_path, autostart=False),
                       mode="mock").test_client()
    w = c.get("/prep/prep-worker.js")
    assert w.status_code == 200 and w.mimetype == "text/javascript" and b"prepare_file" in w.data
    assert c.get("/prep/agentmeter-prep.zip").data == B.OUT.read_bytes()
    assert c.get("/prep/wheels/scapy-2.8.0-py3-none-any.whl").status_code == 200
    assert c.get("/prep/../config.yaml").status_code == 404 and c.get("/prep/other.js").status_code == 404
