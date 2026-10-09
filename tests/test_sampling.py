"""Phase 43b §3: representative sampling of large inputs (replaces first-N).

CSV: one streaming pass, stratified reservoir across the WHOLE file.
PCAP: K time windows spread evenly across the capture's span; full parse when
      the capture fits the packet budget. Small inputs are unchanged.
"""
from __future__ import annotations

import io
import struct

import pandas as pd
import pytest

from agentmeter.config import PROJECT_ROOT
from agentmeter.ingest import csv_input, pcap_window
from agentmeter.ingest.csv_input import load_csv, read_pool
from agentmeter.ingest.pcap import PcapValidationError
from agentmeter.ingest.run import process_csv, process_pcap

from synth import big_csv, big_pcap

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv"
SAMPLE_PCAP = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"
KW = dict(low_memory=False, encoding_errors="replace", skipinitialspace=True)


# ---------------------------------------------------------------------------
# regression: normal small files give the same results as before Phase 43b
# ---------------------------------------------------------------------------
# Selected flow ids of the pre-43b code on the committed samples (verified
# byte-identical selected_flows.csv / labels.csv against commit 3f39381).
PRE_43B_CSV = ['row0000035', 'row0000008', 'row0000027', 'row0000011', 'row0000012', 'row0000014',
               'row0000023', 'row0000033', 'row0000007', 'row0000004', 'row0000037', 'row0000019']
PRE_43B_CSV_6 = ['row0000027', 'row0000022', 'row0000015', 'row0000012', 'row0000023', 'row0000033',
                 'row0000007', 'row0000005', 'row0000010', 'row0000041', 'row0000042', 'row0000020']
PRE_43B_PCAP = ['f00021', 'f00019', 'f00013', 'f00012', 'f00007', 'f00011', 'f00031', 'f00018']


def test_small_csv_pool_is_the_whole_file_unchanged():
    pool, s = read_pool(SAMPLE_CSV, max_rows=500_000, label_col="Label", read_kw=KW)
    full = pd.read_csv(SAMPLE_CSV, **KW)
    full.columns = [c.strip() for c in full.columns]
    pd.testing.assert_frame_equal(pool, full)
    assert s["method"] == "full_file" and s["rows_in_file"] == s["pool_rows"] == 43


def test_small_inputs_select_exactly_what_they_did_before(tmp_path):
    a = process_csv(SAMPLE_CSV, name="a", out_root=tmp_path, max_flows=12, max_rows=500_000)
    b = process_csv(SAMPLE_CSV, name="b", out_root=tmp_path, max_flows=12, max_rows=500_000, other_attack=True)
    assert list(a["input"].selected["flow_id"]) == PRE_43B_CSV
    assert list(b["input"].selected["flow_id"]) == PRE_43B_CSV_6
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    p = process_pcap(SAMPLE_PCAP, name="p", out_root=tmp_path, max_flows=8, max_packets=100_000,
                     max_pool_flows=20_000)
    assert list(p["input"].selected["flow_id"]) == PRE_43B_PCAP
    assert p["manifest"]["sampling"]["method"] == "full_capture"


# ---------------------------------------------------------------------------
# CSV: whole-file stratified reservoir
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def csv_5k(tmp_path_factory):
    return big_csv(tmp_path_factory.mktemp("csv") / "big.csv", 5000, rare_tail=3)


def test_csv_pool_is_drawn_from_across_the_whole_file(csv_5k):
    pool, s = read_pool(csv_5k, max_rows=500, label_col="Label", read_kw=KW)
    assert s["method"] == "stratified_reservoir" and s["rows_in_file"] == 5000 and len(pool) == 500
    idx = pool.index.to_numpy()
    assert idx.min() < 500 and idx.max() > 4500                    # early AND late rows
    assert (idx > 2500).sum() > 150                                 # not a first-N cut
    assert list(idx) == sorted(idx)                                 # file order kept
    labels = pool["Label"].str.strip().value_counts()
    assert labels.get("PortScan") == 3                              # a class only at the END survives
    assert set(s["strata_in_file"]) == set(s["strata_in_pool"])     # no label lost


def test_csv_pool_is_stratified_and_reproducible(csv_5k):
    a, s = read_pool(csv_5k, max_rows=500, label_col="Label", read_kw=KW)
    b, _ = read_pool(csv_5k, max_rows=500, label_col="Label", read_kw=KW)
    pd.testing.assert_frame_equal(a, b)                             # seeded: same pool every time
    c, _ = read_pool(csv_5k, max_rows=500, label_col="Label", read_kw=KW, seed=7)
    assert not a.index.equals(c.index)
    big = {k: v for k, v in s["strata_in_pool"].items() if s["strata_in_file"][k] > 100}
    assert max(big.values()) - min(big.values()) <= 1               # water-filled equal shares


def test_csv_pool_with_small_chunks_still_spans_the_file(csv_5k, monkeypatch):
    monkeypatch.setattr(csv_input, "POOL_CHUNK_ROWS", 700)          # many trims during the pass
    pool, s = read_pool(csv_5k, max_rows=300, label_col="Label", read_kw=KW)
    assert len(pool) == 300 and pool.index.max() > 4500 and pool.index.min() < 700


def test_unlabelled_csv_gets_a_uniform_reservoir(tmp_path):
    p = big_csv(tmp_path / "u.csv", 3000)
    df = pd.read_csv(p).drop(columns=[" Label"])
    df.to_csv(p, index=False)
    pool, s = read_pool(p, max_rows=200, label_col=None, read_kw=KW)
    assert len(pool) == 200 and s["stratified_by"] is None and pool.index.max() > 2500


def test_load_csv_reports_the_sampling(csv_5k):
    data = load_csv(csv_5k, max_rows=500)
    r = data.report
    assert r["rows_read"] == 5000 and r["rows_in_pool"] == 500
    assert r["sampling"]["method"] == "stratified_reservoir"
    assert r["sampling"]["class_counts_in_file"]["Port Scanning"] >= 3
    assert data.labels["label"].value_counts()["Port Scanning"] >= 3


def test_sampled_csv_flows_through_selection(csv_5k, tmp_path):
    out = process_csv(csv_5k, name="s", out_root=tmp_path, max_flows=20, max_rows=500)
    assert out["input"].metadata["rows_selected"] == 20
    assert "stratified random sample" in (tmp_path / "s" / "schema_report.md").read_text()
    ids = [int(f[3:]) for f in out["input"].selected["flow_id"]]
    assert max(ids) > 2500                                          # selection reaches late rows


# ---------------------------------------------------------------------------
# PCAP: time windows
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pcap_5k(tmp_path_factory):
    return big_pcap(tmp_path_factory.mktemp("pcap") / "big.pcap", 5000, span_s=1000.0)


def test_capture_index_counts_packets_and_span_without_decoding(pcap_5k):
    idx = pcap_window.index(pcap_5k)
    assert idx.file_format == "pcap" and idx.packets == 10_000
    assert idx.span_s == pytest.approx(1000.0 - 0.2, abs=0.01)


def test_window_plan_is_evenly_spread(pcap_5k, tmp_path):
    idx = pcap_window.index(pcap_5k)
    plan = pcap_window.plan_windows(idx, k=5, budget=1000)
    starts = [w.start_ts - idx.first_ts for w in plan.windows]
    assert starts == pytest.approx([0, 0.2 * idx.span_s, 0.4 * idx.span_s, 0.6 * idx.span_s, 0.8 * idx.span_s])
    n = pcap_window.write_windows(pcap_5k, tmp_path / "w.pcap", plan)
    assert n == 1000 and all(w.packets_taken == 200 for w in plan.windows)
    assert pcap_window.index(tmp_path / "w.pcap").packets == 1000
    for w in plan.windows:                                          # each window starts at its slot
        assert w.first_packet_ts >= w.start_ts and w.first_packet_ts - w.start_ts < 1.0


def test_large_capture_is_sampled_across_its_span(pcap_5k, tmp_path):
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    out = process_pcap(pcap_5k, name="w", out_root=tmp_path, max_flows=40, max_packets=1000,
                       windows=5, max_pool_flows=20_000)
    s = out["manifest"]["sampling"]
    assert s["method"] == "time_windows" and s["window_count"] == 5 and s["packets_parsed"] == 1000
    assert s["packets_in_capture"] == 10_000 and len(s["windows"]) == 5
    flows = out["flows"]
    assert 400 <= len(flows) <= 520                                 # ~100 flows per window
    octets = sorted({int(ip.split(".")[2]) * 256 + int(ip.split(".")[3]) for ip in flows["src_ip"]})
    fifths = {o * 5 // 5000 for o in octets}                       # flow i was sent at i/5000 of the span
    assert fifths == {0, 1, 2, 3, 4}                                # flows from every fifth of the capture
    assert out["input"].metadata["rows_selected"] == 40
    assert not list((tmp_path / ".work").glob("*"))                 # reduced capture cleaned up


def test_capture_within_budget_is_processed_in_full(tmp_path):
    small = big_pcap(tmp_path / "s.pcap", 50, span_s=10)
    idx = pcap_window.index(small)
    assert idx.packets == 100
    pytest.importorskip("scapy")
    pytest.importorskip("cicflowmeter")
    out = process_pcap(small, name="f", out_root=tmp_path, max_flows=10, max_packets=100)
    assert out["manifest"]["sampling"]["method"] == "full_capture"
    assert out["manifest"]["sampling"]["windows"] == []


def test_pcapng_is_indexed_and_windowed(tmp_path):
    scapy = pytest.importorskip("scapy")
    from scapy.layers.inet import IP, UDP
    from scapy.layers.l2 import Ether
    from scapy.utils import PcapNgWriter
    pk = []
    for i in range(400):
        p = Ether() / IP(src=f"10.0.{i // 256}.{i % 256}", dst="192.168.1.1") / UDP(sport=1000 + i, dport=53)
        p.time = 1_600_000_000 + i * 0.5
        pk.append(p)
    path = tmp_path / "c.pcapng"
    w = PcapNgWriter(str(path))
    for p in pk:
        w.write(p)
    w.close()
    idx = pcap_window.index(path)
    assert idx.file_format == "pcapng" and idx.packets == 400
    assert idx.span_s == pytest.approx(199.5, abs=0.01)
    plan = pcap_window.plan_windows(idx, k=4, budget=40)
    assert pcap_window.write_windows(path, tmp_path / "w.pcapng", plan) == 40
    from scapy.utils import rdpcap
    got = rdpcap(str(tmp_path / "w.pcapng"))
    assert len(got) == 40 and float(got[-1].time) > 1_600_000_000 + 150     # last window kept
    _ = scapy


def test_truncated_capture_is_rejected_by_the_index(tmp_path, pcap_5k):
    bad = tmp_path / "t.pcap"
    bad.write_bytes(pcap_5k.read_bytes()[:-7])
    with pytest.raises(PcapValidationError, match="truncated"):
        pcap_window.index(bad)
    huge = tmp_path / "h.pcap"
    huge.write_bytes(pcap_5k.read_bytes()[:24] + struct.pack("<IIII", 0, 0, 2 ** 31, 2 ** 31))
    with pytest.raises(PcapValidationError, match="corrupt"):
        pcap_window.index(huge)


# ---------------------------------------------------------------------------
# honest reporting: classes absent after sampling (summary + manifest + PDF)
# ---------------------------------------------------------------------------
def test_classes_absent_after_selection_are_reported(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from test_prepare import _app, prepare
    c, mgr = _app(tmp_path, monkeypatch)
    sid = prepare(c, mgr, SAMPLE_CSV, max_flows="3")["prepared"]     # 3 flows < 5 classes
    s = c.get(f"/api/prepared/{sid}").get_json()
    lost = s["class_counts"]["lost_in_selection"]
    assert len(lost) >= 2
    assert any("absent from the prepared set" in n and lost[0] in n for n in s["notes"])
    j = c.post("/api/jobs", json={"run": sid, "models": ["mistralai/Mistral-7B-Instruct-v0.3"], "provider": "mock"})
    jid = j.get_json()["job_id"]
    assert mgr.wait(jid, timeout=120)["status"] == "done"
    from pypdf import PdfReader
    t = "\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(c.get(f"/api/jobs/{jid}/report.pdf").data)).pages)
    assert "Sampling" in t and "all 43 rows of the file" in t
    assert "Classes absent after sampling" in t and lost[0] in t
    assert "Prepared set" in t and sid.split("/")[1][:20] in t.replace("\n", "")
