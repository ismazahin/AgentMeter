"""Phase 43: web-service guards for large inputs (CPU, mock).

  * an upload over the configured limit is rejected with 413 BEFORE its body is
    read, with a message naming the limit;
  * a large but accepted CSV/PCAP is parsed up to the configured cap, the summary
    says the cap was applied, the rule-base still selects a bounded set, and the
    run proceeds normally;
  * normal-size files are unaffected; the locked study DB/dataset are untouched.

Caps are set tiny via the env overrides so the repo's small samples exercise them.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io

import pandas as pd
import pytest
import yaml

from agentmeter.config import PROJECT_ROOT
from agentmeter.server import service_api
from agentmeter.server.jobs import JobManager

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
DATASET = PROJECT_ROOT / "data" / "cicids_full_300.csv"
DATASET_SHA256 = "ef1b13787e2c87602033f54d985355515408b4f930c9648cd1fb29cb540392b5"
LIMIT_ENV = ("AGENTMETER_MAX_UPLOAD_MB", "AGENTMETER_MAX_UPLOAD_BYTES", "AGENTMETER_MAX_PCAP_PACKETS",
             "AGENTMETER_MAX_PCAP_FLOWS", "AGENTMETER_MAX_CSV_ROWS")


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def make(tmp_path, monkeypatch):
    """make(**env) -> (client, manager, tmp): a fresh app built AFTER the env is set
    (limits are read once at startup)."""
    pytest.importorskip("flask")
    monkeypatch.delenv("AGENTMETER_SERVICE_PROVIDER", raising=False)
    for k in LIMIT_ENV:
        monkeypatch.delenv(k, raising=False)

    def build(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        from agentmeter import pull_eval
        spec = importlib.util.spec_from_file_location("pull_eval_server",
                                                      PROJECT_ROOT / "scripts" / "pull_eval_server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        base = tmp_path / "cfg.yaml"
        base.write_text(yaml.safe_dump({"run": {"models": ["mock/m"]}, "model": {"provider": "mock"},
                                        "dataset": {}, "pipeline": {}, "classes": [], "scoring": {},
                                        "storage": {}}))
        pmgr = pull_eval.JobManager(base_config=str(base), canonical_json=str(tmp_path / "c.json"),
                                    out_dir=str(tmp_path / "pulls"))
        mgr = JobManager(jobs_dir=tmp_path / "jobs", results_root=tmp_path / "results",
                         gpu_available=lambda: False)
        client = srv.create_app(pmgr, local_results_dir=str(tmp_path / "results"),
                                job_manager=mgr).test_client()
        return client, mgr, tmp_path

    return build


def upload(client, path, **form):
    data = {"file": (io.BytesIO(path.read_bytes()), path.name), **form}
    return client.post("/api/ingest", data=data, content_type="multipart/form-data")


class _CountingStream:
    """A request body that records how much of it the server reads."""

    def __init__(self, size: int):
        self.size, self.read_bytes = size, 0

    def read(self, n=-1):
        n = self.size - self.read_bytes if n is None or n < 0 else min(n, self.size - self.read_bytes)
        self.read_bytes += n
        return b"x" * n

    def readline(self, n=-1):
        return self.read(min(n, 64) if n and n > 0 else 64)


# ---------------------------------------------------------------------------
# limits: config + overrides
# ---------------------------------------------------------------------------
def test_limits_come_from_config_with_env_overrides(make, monkeypatch):
    c, _, _ = make()
    lim = c.get("/api/service/config").get_json()["limits"]
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text())["service"]
    assert lim["max_upload_mb"] == cfg["max_upload_mb"] and lim["max_upload_mb"] < 100   # under Cloudflare's 100 MB
    assert lim["max_upload_bytes"] == cfg["max_upload_mb"] * 1024 ** 2
    assert lim["max_upload_label"] == f"{cfg['max_upload_mb']} MB"
    assert (lim["max_pcap_flows"], lim["max_csv_rows"], lim["max_pcap_packets"]) == \
        (cfg["max_pcap_flows"], cfg["max_csv_rows"], cfg["max_pcap_packets"])
    assert lim["max_url_download_gb"] == cfg["max_url_download_gb"] == 10
    assert lim["max_url_download_bytes"] == 10 * 1024 ** 3 and lim["max_url_download_label"] == "10 GB"
    monkeypatch.setenv("AGENTMETER_MAX_CSV_ROWS", "7")
    monkeypatch.setenv("AGENTMETER_MAX_UPLOAD_BYTES", "12345")      # pre-Phase-43 override still wins
    lim = service_api.service_limits()
    assert lim["max_csv_rows"] == 7 and lim["max_upload_bytes"] == 12345
    monkeypatch.setenv("AGENTMETER_MAX_CSV_ROWS", "0")
    with pytest.raises(service_api.ServiceError, match="must be positive"):
        service_api.service_limits()


# ---------------------------------------------------------------------------
# 1. upload guard
# ---------------------------------------------------------------------------
def test_oversized_upload_is_rejected_before_the_body_is_read(make):
    c, _, tmp = make(AGENTMETER_MAX_UPLOAD_MB=1)
    body = _CountingStream(5 * 1024 ** 2)
    r = c.post("/api/ingest", content_type="multipart/form-data; boundary=b",
               environ_overrides={"wsgi.input": body, "CONTENT_LENGTH": str(body.size)})
    j = r.get_json()
    assert r.status_code == 413 and j["code"] == "too_large"
    assert "upload limit is 1 MB" in j["error"]
    assert body.read_bytes == 0                       # rejected on the declared size alone
    assert not (tmp / "results" / "uploads").exists() or not any((tmp / "results" / "uploads").iterdir())


def test_oversized_real_multipart_upload_gets_the_clear_message(make):
    c, _, tmp = make(AGENTMETER_MAX_UPLOAD_MB=1)
    r = c.post("/api/ingest", data={"file": (io.BytesIO(b"a" * (3 * 1024 ** 2)), "big.csv")},
               content_type="multipart/form-data")
    assert r.status_code == 413 and r.get_json()["code"] == "too_large"
    assert "1 MB" in r.get_json()["error"]


def test_flask_body_cap_is_set_and_returns_json(make):
    c, _, _ = make(AGENTMETER_MAX_UPLOAD_MB=1)
    assert c.application.config["MAX_CONTENT_LENGTH"] == 1024 ** 2 + service_api._MULTIPART_SLACK
    # A body werkzeug itself cuts off (here: on another /api route) still gets JSON, not HTML.
    r = c.post("/api/jobs", data=b"x" * (3 * 1024 ** 2), content_type="application/json")
    assert r.status_code == 413 and r.get_json()["code"] == "too_large"


def test_stream_copy_stops_at_the_limit_and_cleans_up(tmp_path):
    dest = tmp_path / "u.bin"
    with pytest.raises(service_api.ServiceError) as e:
        service_api.save_stream(io.BytesIO(b"z" * 5000), dest, max_bytes=4096)
    assert e.value.status == 413 and not dest.exists()
    n, sha = service_api.save_stream(io.BytesIO(b"z" * 4096), dest, max_bytes=4096)
    assert n == 4096 and sha == hashlib.sha256(b"z" * 4096).hexdigest()


def test_upload_at_the_limit_is_accepted(make):
    c, _, _ = make(AGENTMETER_MAX_UPLOAD_BYTES=SAMPLE_CSV.stat().st_size)
    r = upload(c, SAMPLE_CSV, max_flows="5")
    assert r.status_code == 201 and r.get_json()["large_input"] == {"capped": False}


# ---------------------------------------------------------------------------
# 2. extraction guard — CSV rows
# ---------------------------------------------------------------------------
def test_large_csv_is_sampled_across_the_file_and_the_run_proceeds(make):
    before = {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")}
    c, mgr, tmp = make(AGENTMETER_MAX_CSV_ROWS=20)
    s = upload(c, SAMPLE_CSV, max_flows="8").get_json()
    big = s["large_input"]
    assert big["capped"] is True and (big["unit"], big["cap"]) == ("rows", 20)
    assert "drawn from all 43 rows" in big["message"] and "not the first rows" in big["message"]
    assert s["rows_read"] == 43 and s["rows_in_pool"] == 20 and s["rows_selected"] <= 8
    assert s["sampling"]["method"] == "stratified_reservoir"
    name = s["run"].split("/")[1]
    assert "stratified random sample" in (tmp / "results" / "csv_runs" / name / "schema_report.md").read_text()
    assert c.get(f"/api/runs/{s['run']}").get_json()["large_input"] == big   # survives a reload
    cfg = c.get("/api/service/config").get_json()
    j = c.post("/api/jobs", json={"run": s["run"], "models": cfg["canonical_models"][:1],
                                  "provider": cfg["provider"], "base_config": cfg["base_config"]})
    assert j.status_code == 202
    jid = j.get_json()["job_id"]
    mgr.wait(jid, timeout=120)
    assert c.get(f"/api/jobs/{jid}").get_json()["status"] == "done"
    assert {str(p): _sha(p) for p in (PROJECT_ROOT / "results").glob("*.db")} == before
    assert _sha(DATASET) == DATASET_SHA256


def test_csv_exactly_at_the_cap_is_not_reported_as_capped(make):
    c, _, _ = make(AGENTMETER_MAX_CSV_ROWS=43)          # the sample has exactly 43 data rows
    s = upload(c, SAMPLE_CSV, max_flows="12").get_json()
    assert s["large_input"] == {"capped": False} and s["rows_read"] == 43


def test_normal_csv_unaffected_by_the_defaults(make):
    c, _, _ = make()
    s = upload(c, SAMPLE_CSV, max_flows="12").get_json()
    assert (s["rows_read"], s["rows_usable"], s["rows_selected"]) == (43, 40, 12)
    assert s["large_input"] == {"capped": False} and s["file_size_bytes"] == SAMPLE_CSV.stat().st_size


# ---------------------------------------------------------------------------
# 2. extraction guard — PCAP flows / packets
# ---------------------------------------------------------------------------
def test_large_pcap_pool_is_a_random_subsample_not_the_first_flows(make):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    c, _, tmp = make(AGENTMETER_MAX_PCAP_FLOWS=10)
    s = upload(c, SAMPLE_PCAP, max_flows="8").get_json()
    big = s["large_input"]
    assert big["capped"] is True and (big["unit"], big["cap"]) == ("flows", 10)
    assert "random pool of 10" in big["message"]
    assert s["flows_extracted"] == 41 and s["rows_total"] == 10 and s["rows_selected"] == 8
    pool = pd.read_csv(tmp / "results" / "pcap_runs" / s["run"].split("/")[1] / "flows.csv")
    ids = sorted(int(f[1:]) for f in pool["flow_id"])
    assert ids != list(range(10)) and max(ids) > 20            # drawn from across the capture


def test_large_pcap_is_sampled_in_time_windows(make):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    c, _, _ = make(AGENTMETER_MAX_PCAP_PACKETS=100, AGENTMETER_PCAP_WINDOWS=4)
    s = upload(c, SAMPLE_PCAP, max_flows="8").get_json()
    big, samp = s["large_input"], s["sampling"]
    assert big["capped"] is True and big["unit"] == "windows" and "4 time windows" in big["message"]
    assert samp["method"] == "time_windows" and samp["window_count"] == 4
    assert s["packets"] == 325 and s["packets_parsed"] <= 100 and s["rows_selected"] <= 8
    w = samp["windows"]
    span = samp["capture_last_ts"] - samp["capture_first_ts"]
    assert w[0]["start_ts"] == samp["capture_first_ts"]
    assert w[-1]["start_ts"] == pytest.approx(samp["capture_first_ts"] + 0.75 * span)
    assert all(x["packets_taken"] <= samp["budget_per_window"] for x in w)


def test_normal_pcap_unaffected_by_the_defaults(make):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    c, _, _ = make()
    s = upload(c, SAMPLE_PCAP, max_flows="8").get_json()
    assert (s["packets"], s["flows_extracted"], s["rows_selected"]) == (325, 41, 8)
    assert s["large_input"] == {"capped": False}


# ---------------------------------------------------------------------------
# 3. UI hooks
# ---------------------------------------------------------------------------
def test_service_page_shows_the_limit_and_the_cap_notice(make):
    c, _, _ = make()
    html = c.get("/service").get_data(as_text=True)
    for hook in ('id="svc-limits"', "svc-large-input", "max_upload_label", "large_input"):
        assert hook in html
