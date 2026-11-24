"""The full PDF session report (agentmeter/session/report_html.py, 2026-11-24 design).

Fixtures in tests/fixtures/report/ are real /api/jobs/<id>/result outputs of mock runs (1, 2 and 4
models) plus one with synthetic GPU readings and the RTX 3090 numbers of DESIGN_BRIEF.md section 6
patched in (synth-real-two, TEST ONLY), so every real-GPU code path renders.
"""
import html
import io
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("weasyprint")
pytest.importorskip("quickjs")

from agentmeter.session import report_charts as ch  # noqa: E402
from agentmeter.session.report_html import Report, build_report_html, build_report_pdf, ftime  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "report"
NAMES = ["mock-one", "mock-two", "mock-four", "synth-real-two"]


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def text_of(h):
    body = h[h.index("<body>"):]
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))


@pytest.fixture(scope="module")
def rendered():
    return {n: build_report_html(load(n), job={"job_id": f"job_{n}"}) for n in NAMES}


@pytest.mark.parametrize("name", NAMES)
def test_every_part_is_there(rendered, name):
    t = text_of(rendered[name])
    for part in ("In plain words", "Every measure, one chart each", "How each number is measured", "How a winner is decided",
                 "Overview", "Ranking", "Output reliability", "Performance", "Agents", "Cost and energy",
                 "Recommendations", "Data", "Methodology", "Words used in this report"):
        assert part in t, part
    assert f"job_{name}" in t


@pytest.mark.parametrize("name", NAMES)
def test_no_leaked_placeholders_or_banned_punctuation(rendered, name):
    t = text_of(rendered[name])
    for bad in (r"\bNone\b", r"\bnan\b", r"\bNaN\b", r"undefined", "—", ";"):
        assert not re.search(bad, t), (bad, re.search(r".{60}" + bad + r".{40}", t))


def test_best_only_where_the_session_found_a_real_difference(rendered):
    t = text_of(rendered["synth-real-two"])
    # the only decided measure is working memory (Qwen); speed and tokens are ties
    assert t.count("Best: ") == 1 and "Best: Qwen2.5 7B, 35.5% better" in t
    assert "Tie" in t and "not tested" in t
    assert "Best: " not in text_of(rendered["mock-two"])          # a tie session names no best measure


def test_front_numbers_are_the_stored_ones(rendered):
    res = load("synth-real-two")
    t = text_of(rendered["synth-real-two"])
    for m in res["per_model"]:
        e = m["efficiency"]["end_to_end"]
        assert ftime(e["mean_latency_s"]) in t and ftime(e["p95_latency_s"]) in t
        assert f"{e['mean_tokens_per_flow']:,.0f}" in t
        assert f"{m['saw_efficiency_only']['composite_100']:.2f}" in t
    for r in res["memory"]["per_model"]:
        assert f"{r['total_peak_mb']:,.0f} MB" in t
    for r in res["cost_energy"]["per_model"]:
        assert f"${r['cost_per_1k_flows_usd']:,.2f}" in t
    # the picks include the tested win and the explanation of the untested speed lean
    assert "Pick Qwen2.5 7B if" in t and "working memory" in t
    assert "lean, not a result" in t


def test_missing_values_say_not_measured(rendered):
    t = text_of(rendered["mock-two"])
    assert "not measured" in t and "mock/CPU run" in t
    assert "Demo run" in t                                         # the demo banner on page 1


def test_single_model_report(rendered):
    t = text_of(rendered["mock-one"])
    assert "Is this model a fit" in t and "Pick " not in t and "Best: " not in t
    assert "One model, so no head-to-head comparison" in t
    assert "Target" in t                                           # bars compare with the score targets


def test_the_web_rules_word_the_answers(rendered):
    """Section answers come from web/insights.js (sectionAnswers), the same sentences as the web."""
    res = load("mock-two")
    rep = Report(res)
    t = text_of(rendered["mock-two"])
    answers = rep.answers
    assert answers, "insights.js answered nothing"
    for q in ("How fast is each model?", "Is the difference real?", "Did every flow get a usable answer?"):
        assert q in t and rep.plain(answers[q]) in t


def test_pdf_renders_with_page_numbers():
    pdf = build_report_pdf(load("mock-two"), job={"job_id": "job_x"})
    assert pdf[:5] == b"%PDF-"
    pypdf = pytest.importorskip("pypdf")
    r = pypdf.PdfReader(io.BytesIO(pdf))
    assert 10 <= len(r.pages) <= 40
    last = r.pages[-1].extract_text()
    assert f"Page {len(r.pages)} of {len(r.pages)}" in last


def test_charts_are_plain_svg():
    s = ch.scatter([dict(label="A", x=1.0, y=2.0, color="#000"), dict(label="B", x=2.0, y=1.0, color="#111")],
                   x_label="x", y_label="y", x_fmt=str, y_fmt=str)
    assert s.startswith("<svg") and s.count("<circle") == 2 and "<script" not in s
    assert ch.scatter([], x_label="x", y_label="y", x_fmt=str, y_fmt=str) == ""
    assert ch.model_colors(["Qwen/Qwen2.5-7B", "meta-llama/Llama-3"]) == {"Qwen/Qwen2.5-7B": "#0B86A6", "meta-llama/Llama-3": "#2E5BD8"}


def test_route_serves_the_full_report_and_falls_back(monkeypatch, tmp_path):
    """report_pdf_bytes: the full report by default, the plain one when it fails or is switched off."""
    from agentmeter.server import jobs_api
    res = load("mock-two")
    run = tmp_path / "run"
    run.mkdir()

    class Mgr:
        def result(self, jid):
            return res

        def get(self, jid):
            return {"job_id": jid, "run_dir": str(run)}

    calls = []
    import agentmeter.session.report_html as rh
    real = rh.build_report_pdf
    monkeypatch.delenv("AGENTMETER_PDF_LAYOUT", raising=False)
    monkeypatch.setattr(rh, "build_report_pdf", lambda r, **kw: calls.append("full") or real(r, **kw))
    full = jobs_api.report_pdf_bytes(Mgr(), "job_a")
    assert calls == ["full"] and full[:5] == b"%PDF-"

    def boom(r, **kw):
        raise RuntimeError("no pango")
    monkeypatch.setattr(rh, "build_report_pdf", boom)
    plain = jobs_api.report_pdf_bytes(Mgr(), "job_a")
    assert plain[:5] == b"%PDF-" and plain != full                  # the plain ReportLab report

    monkeypatch.setattr(rh, "build_report_pdf", lambda r, **kw: calls.append("full2"))
    monkeypatch.setenv("AGENTMETER_PDF_LAYOUT", "plain")
    assert jobs_api.report_pdf_bytes(Mgr(), "job_a")[:5] == b"%PDF-" and "full2" not in calls
