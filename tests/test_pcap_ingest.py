"""PCAP ingestion + CICFlowMeter flow extraction + end-to-end input layer.

CPU only, no GPU, no LLM, no network. The capture is generated with scapy
(agentmeter/ingest/sample.py), so nothing has to be downloaded.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("scapy", reason="PCAP layer needs requirements-pcap.txt")
pytest.importorskip("cicflowmeter", reason="pip install --no-deps cicflowmeter==0.2.0")

from agentmeter.config import PROJECT_ROOT  # noqa: E402
from agentmeter.ingest import feature_map as fm  # noqa: E402
from agentmeter.ingest.flows import extract_flows  # noqa: E402
from agentmeter.ingest.pcap import PcapValidationError, validate_pcap  # noqa: E402
from agentmeter.ingest.run import process_pcap, safe_run_name  # noqa: E402
from agentmeter.ingest.sample import EXPECTED_FLOWS, build_packets, write_sample_pcap  # noqa: E402

COMMITTED_SAMPLE = PROJECT_ROOT / "data" / "sample_pcaps" / "sample_small.pcap"


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    return write_sample_pcap(tmp_path_factory.mktemp("pcap") / "sample.pcap")


@pytest.fixture(scope="module")
def extraction(sample):
    return extract_flows(sample)


def test_committed_sample_matches_the_generator(sample):
    assert COMMITTED_SAMPLE.read_bytes() == sample.read_bytes()


# --- ingestion / validation ------------------------------------------------------
def test_valid_capture_reports_stats(sample):
    st = validate_pcap(sample)
    assert st.file_format == "pcap"
    assert st.packet_count == len(build_packets()) == 325
    assert st.file_size_bytes == sample.stat().st_size
    assert st.duration_s > 90                       # the long-lived flow spans ~95 s
    assert st.tcp_packets == 266 and st.udp_packets == 55 and st.other_packets == 4
    assert st.truncated_at is None


def test_pcapng_is_accepted_and_gives_the_same_flows(tmp_path, extraction):
    from scapy.utils import wrpcapng

    p = tmp_path / "sample.pcapng"
    wrpcapng(str(p), build_packets())
    assert validate_pcap(p).file_format == "pcapng"
    assert len(extract_flows(p).flows) == len(extraction.flows)


@pytest.mark.parametrize("content, msg", [
    (b"src_ip,dst_ip\n1.2.3.4,5.6.7.8\n", "not a pcap/pcapng capture"),
    (b"", "not a pcap/pcapng capture"),
    (b"\xd4\xc3\xb2\xa1" + b"\x00\xff" * 40, "bad global header"),
    (b"\xd4\xc3\xb2\xa1\x02\x00", "header too short"),
])
def test_non_pcap_and_corrupt_files_are_rejected(tmp_path, content, msg):
    p = tmp_path / "upload.pcap"
    p.write_bytes(content)
    with pytest.raises(PcapValidationError, match=msg):
        validate_pcap(p)


def test_lenient_scapy_cases_are_still_rejected(tmp_path, sample):
    # scapy only WARNS on these and stops as if at EOF; validation must not accept them.
    cut = tmp_path / "cut.pcap"
    cut.write_bytes(sample.read_bytes()[:-30])          # last record cut off mid-packet
    with pytest.raises(PcapValidationError, match="truncated"):
        validate_pcap(cut)
    weird = bytearray(sample.read_bytes())
    weird[20:24] = (0xFF00FF00).to_bytes(4, "little")   # valid header, unknown link type
    bad_link = tmp_path / "link.pcap"
    bad_link.write_bytes(bytes(weird))
    with pytest.raises(PcapValidationError, match="unknown LL type"):
        validate_pcap(bad_link)


def test_empty_capture_and_missing_file_and_size_limit_are_rejected(tmp_path, sample):
    from scapy.utils import wrpcap

    empty = tmp_path / "empty.pcap"
    wrpcap(str(empty), [])
    with pytest.raises(PcapValidationError, match="no packets"):
        validate_pcap(empty)
    with pytest.raises(PcapValidationError, match="not a file"):
        validate_pcap(tmp_path / "nope.pcap")
    with pytest.raises(PcapValidationError, match="exceeds"):
        validate_pcap(sample, max_bytes=1000)


def test_max_packets_truncates_and_says_so(sample):
    st = validate_pcap(sample, max_packets=50)
    assert st.packet_count == 50 and st.truncated_at == 50
    assert any("max_packets" in n for n in st.notes)


# --- flow extraction -------------------------------------------------------------
def test_flows_have_meta_plus_the_78_cic_columns_in_order(extraction):
    df = extraction.flows
    assert len(df) == EXPECTED_FLOWS == 41
    assert list(df.columns) == fm.META_COLUMNS + fm.CIC_FEATURES
    assert df["flow_id"].is_unique
    assert df["protocol_name"].value_counts().to_dict() == {"UDP": 27, "TCP": 14}
    assert extraction.packets_used == 320 and extraction.packets_skipped == 5
    assert not df[fm.CIC_FEATURES].isna().any().any()


def test_flow_values_follow_cic_ids2017_semantics(extraction):
    df = extraction.flows.set_index("Destination Port")
    bulk = df.loc[8080.0]
    # S, A, 60 data, FA forward; SA, 60 data, FA backward.
    assert (bulk["Total Fwd Packets"], bulk["Total Backward Packets"]) == (63.0, 62.0)
    ssh = df.loc[22.0]
    assert ssh["Flow Duration"] >= 60_000_000       # microseconds, as in CIC-IDS2017
    assert ssh["Flow IAT Max"] >= 4_000_000          # IATs in microseconds too
    dns = extraction.flows[extraction.flows["Destination Port"] == 53.0].iloc[0]
    assert dns["Init_Win_bytes_forward"] == 0 and dns["Total Backward Packets"] == 1


def test_feature_map_for_real_extraction_has_no_gaps(extraction):
    rep = fm.mapping_report(extraction.extracted_keys)
    assert rep["counts"]["missing"] == 0
    assert rep["unmapped_extractor_keys"] == []
    assert rep["comparable"] == rep["counts"]["direct"] + rep["counts"]["duplicate"]
    assert rep["counts"]["semantic_diff"] > 0        # the gap is reported, not hidden


# --- end to end ------------------------------------------------------------------
def _db_hashes() -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (PROJECT_ROOT / "results").glob("*.db")}


def test_process_pcap_writes_only_its_run_folder(tmp_path, sample):
    before = _db_hashes()
    res = process_pcap(sample, name="../evil name", out_root=tmp_path, max_flows=15)
    out = tmp_path / "evil_name"
    assert sorted(p.name for p in out.iterdir()) == [
        "feature_map.json", "feature_map.md", "flows.csv", "input.json", "manifest.json",
        "selected_flows.csv", "selection_audit.json"]
    assert not list(tmp_path.rglob("*.db"))
    assert _db_hashes() == before                    # study DB(s) untouched

    man = json.loads((out / "manifest.json").read_text())
    assert man["extraction"]["flows"] == 41 and man["selection"]["selected"] == 15
    audit = json.loads((out / "selection_audit.json").read_text())
    assert len(audit["flows"]) == 15 and all(f["reason"] for f in audit["flows"])
    assert "protocol_coverage" in audit["rules_fired"]
    assert "| Destination Port | `dst_port` | direct |" in (out / "feature_map.md").read_text()
    assert len(res["selection"].selected) == 15


def test_safe_run_name_blocks_traversal():
    assert safe_run_name("../../etc/passwd") == "etc_passwd"
    assert safe_run_name("...") == "capture"


def test_cli_accepts_a_capture_and_rejects_a_non_capture(tmp_path, sample, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pcap_ingest", PROJECT_ROOT / "scripts" / "pcap_ingest.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main([str(sample), "--no-write", "--max-flows", "10"]) == 0
    assert "Selection 10 of 41 flows" in capsys.readouterr().out
    bad = tmp_path / "notes.pcap"
    bad.write_text("hello")
    assert cli.main([str(bad), "--no-write"]) == 2
    assert cli.main(["--show-rules"]) == 0
    assert "typical_baseline" in capsys.readouterr().out


def test_input_layer_does_not_import_pipeline_saw_or_study_storage():
    forbidden = ("agentmeter.pipeline", "agentmeter.analysis", "agentmeter.db", "agentmeter.run",
                 "..pipeline", "..analysis", "..db", "..run")
    for path in (PROJECT_ROOT / "agentmeter" / "ingest").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = ("." * node.level) + (node.module or "")
                assert not mod.startswith(forbidden), f"{path.name} imports {mod}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith(forbidden[:4]), f"{path.name} imports {a.name}"


def test_pcap_run_is_flagged_efficiency_only_and_loads_through_the_contract(tmp_path, sample):
    from agentmeter.ingest.unified import SELECTED_COLUMNS, load_input_run

    process_pcap(sample, out_root=tmp_path, max_flows=12)
    meta = json.loads((tmp_path / "sample" / "input.json").read_text())
    assert meta["source_type"] == "pcap" and meta["input_role"] == "raw_pcap"
    assert meta["evaluation_mode"] == "efficiency_only"
    assert meta["capabilities"] == {"resource_efficiency": True, "accuracy": False}
    assert meta["feature_match"]["status"] == "approximate"
    assert any("no ground-truth labels" in r for r in meta["accuracy_unavailable_reasons"])
    assert not (tmp_path / "sample" / "labels.csv").exists()
    run = load_input_run(tmp_path / "sample")
    assert run.labels is None and not run.accuracy_available
    assert list(run.selected.columns) == SELECTED_COLUMNS and len(run.selected) == 12


def test_unified_cli_dispatches_pcap_by_content(tmp_path, sample, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ingest_cli", PROJECT_ROOT / "scripts" / "ingest.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    renamed = tmp_path / "upload.bin"                    # extension is irrelevant: magic bytes decide
    renamed.write_bytes(sample.read_bytes())
    assert cli.main([str(renamed), "--no-write", "--max-flows", "10"]) == 0
    out = capsys.readouterr().out
    assert "raw_pcap -> efficiency_only" in out and "Selection 10 of 41 flows" in out
