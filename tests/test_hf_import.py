"""Phase E §0b: importing the public CIC-IDS2017 CSV from Hugging Face by URL.

  https://huggingface.co/datasets/c01dsnap/CIC-IDS2017/resolve/main/Wednesday-workingHours.pcap_ISCX.csv

The CI/sandbox network cannot reach huggingface.co, so this replays the shape of
that download offline: Hugging Face answers /resolve/ with a relative 307 to its
resolve-cache and then a 302 to a signed CDN URL (cas-bridge.xethub.hf.co) whose
path has no file name, and names the file in Content-Disposition. The body is
tests/fixtures/cic_wednesday_head.csv: the ORIGINAL CIC header (leading spaces,
the duplicated " Fwd Header Length") with rows in the official files' style,
including Infinity/NaN rates and the cp1252 0x96 byte of "Web Attack – ..." labels
(those come from the Thursday file; they are added here to cover the encoding).
Feature values are copied from the committed sample, not recorded from HF.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentmeter.ingest.run import detect_input_type
from agentmeter.server.prepare import prepare_file, summarize
from agentmeter.server.service_api import service_limits
from agentmeter.server.urlfetch import download

from test_url_import import FakeResp, transport

FIXTURE = Path(__file__).parent / "fixtures" / "cic_wednesday_head.csv"
HF_URL = ("https://huggingface.co/datasets/c01dsnap/CIC-IDS2017/resolve/main/"
          "Wednesday-workingHours.pcap_ISCX.csv")
CACHE = "/api/resolve-cache/datasets/c01dsnap/CIC-IDS2017/3f6b2e1a/Wednesday-workingHours.pcap_ISCX.csv"
CDN = ("https://cas-bridge.xethub.hf.co/xet-bridge-us/66c8a1f0/9b1e2c3d4e5f?X-Amz-Algorithm=AWS4-HMAC-SHA256"
       "&X-Amz-Expires=3600&response-content-disposition=inline%3B+filename%2A%3DUTF-8%27%27"
       "Wednesday-workingHours.pcap_ISCX.csv&X-Amz-Signature=deadbeef")
CDN_PATH = CDN.split("hf.co", 1)[1]


@pytest.fixture
def hf(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_ALLOW_LOOPBACK_URLS", raising=False)
    body = FIXTURE.read_bytes()
    routes = {
        ("huggingface.co", "/datasets/c01dsnap/CIC-IDS2017/resolve/main/Wednesday-workingHours.pcap_ISCX.csv"):
            FakeResp(307, {"Location": CACHE}),
        ("huggingface.co", CACHE): FakeResp(302, {"Location": CDN}),
        ("cas-bridge.xethub.hf.co", CDN_PATH): FakeResp(200, {
            "Content-Length": str(len(body)), "Content-Type": "binary/octet-stream",
            "Content-Disposition": ("inline; filename*=UTF-8''Wednesday-workingHours.pcap_ISCX.csv; "
                                    'filename="Wednesday-workingHours.pcap_ISCX.csv";')}, body),
    }
    # Public addresses of the kind these hosts resolve to (CloudFront ranges).
    dns = {"huggingface.co": "18.244.202.68", "cas-bridge.xethub.hf.co": "3.163.189.114"}
    resolver, conn, log = transport(routes, dns)
    d = download(HF_URL, tmp_path / "uploads", max_bytes=10 * 1024 ** 3, timeout_s=60,
                 resolver=resolver, connection_cls=conn, stem="hf")
    return d, log, tmp_path


def test_hf_redirects_pass_the_ssrf_checks_and_name_is_kept(hf):
    d, log, _ = hf
    assert [h for h, _, _ in log] == ["huggingface.co", "huggingface.co", "cas-bridge.xethub.hf.co"]
    assert [ip for _, ip, _ in log] == ["18.244.202.68", "18.244.202.68", "3.163.189.114"]   # pinned
    assert len(d.redirects) == 2 and d.final_url == CDN
    assert d.filename == "Wednesday-workingHours.pcap_ISCX.csv"      # from Content-Disposition
    assert d.detected == "csv" and d.path.suffix == ".csv"            # not mistaken for a PCAP
    assert detect_input_type(d.path) == "csv"


def _prepare(hf, other_attack: bool):
    d, _, root = hf
    sid = prepare_file(d.path, source={"kind": "url", "url": HF_URL, "filename": d.filename,
                                       "final_url": d.final_url, "size_bytes": d.size_bytes,
                                       "sha256": d.sha256},
                       results_root=root / "results", name=f"hf_{other_attack}", max_flows=10,
                       other_attack=other_attack, limits=service_limits())
    kind, name = sid.split("/")
    return summarize(root / "results" / kind / name)


def test_original_cic_header_matches_all_78_features(hf):
    s = _prepare(hf, other_attack=False)
    assert s["source_type"] == "csv" and s["columns_present"] == 78 and s["columns_missing"] == []
    assert s["feature_match"]["status"] == "exact" and s["label_column"] == "Label"
    assert s["rows_read"] == 17 and s["rows_dropped"] == 2                  # the Infinity / NaN rows


def test_labels_map_to_the_study_classes_and_web_attack_is_handled(hf):
    s = _prepare(hf, other_attack=False)
    assert s["class_distribution"] == {"Benign": 4, "DoS Hulk": 4}           # BENIGN, DoS Hulk mapped
    d, _, root = hf
    import json
    man = json.loads((root / "results" / "csv_runs" / "hf_False" / "manifest.json").read_text())
    excluded = man["validation"]["label"]["excluded_out_of_taxonomy"]
    web = {k: v for k, v in excluded.items() if k.startswith("Web Attack")}
    assert set(web) == {"Web Attack � Brute Force", "Web Attack � XSS",
                        "Web Attack � Sql Injection"}                    # decoded, not a crash
    assert {"DoS GoldenEye", "DoS slowloris", "DoS Slowhttptest", "Heartbleed"} <= set(excluded)
    six = _prepare(hf, other_attack=True)
    assert six["class_scheme"]["name"] == "6-class"
    assert six["class_distribution"]["Other Attack"] == 7                     # 4 DoS variants/Heartbleed + 3 web
    assert set(six["other_attack"]["sources"]) >= set(web)
