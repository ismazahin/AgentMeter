"""The PDF report's reading paragraphs and "Why each model performed this way" (design:
docs/superpowers/specs/2026-11-24-why-each-model-design.md): the design facts, the facts per section,
the checks, the explainer's report part and how the PDF shows it."""
import html
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("quickjs")

from agentmeter.session import explain  # noqa: E402
from agentmeter.session.card_facts import build_all, slug  # noqa: E402
from agentmeter.session.model_design import design_from_config, read_design  # noqa: E402
from agentmeter.session.report_js import Insights  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "report"
Q, L = "Qwen/Qwen2.5-7B-Instruct", "meta-llama/Meta-Llama-3-8B-Instruct"
# the published configs of Qwen2.5-3B and Llama-3.2-3B (shapes only), with their index sizes
QCFG = {"num_hidden_layers": 36, "num_attention_heads": 16, "num_key_value_heads": 2, "hidden_size": 2048,
        "vocab_size": 151936, "max_position_embeddings": 32768, "torch_dtype": "bfloat16"}
LCFG = {"num_hidden_layers": 28, "num_attention_heads": 24, "num_key_value_heads": 8, "hidden_size": 3072, "head_dim": 128,
        "vocab_size": 128256, "max_position_embeddings": 131072, "torch_dtype": "bfloat16"}
DESIGN = {Q: design_from_config(QCFG, {"metadata": {"total_size": 6171877376}}),
          L: design_from_config(LCFG, {"metadata": {"total_size": 6425499648}})}


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def facts_of(out, group, key):
    return {f["kind"] + ":" + f["text"] for f in out[group][key]["facts"]}


# --------------------------------------------------------------------------------- design facts
def test_design_from_config_derives_the_attention_cache_and_size():
    q, l = DESIGN[Q], DESIGN[L]
    assert (q["layers"], q["kv_heads"], q["head_dim"], q["vocab_size"]) == (36, 2, 128, 151936)
    assert q["kv_cache_per_token"] == 36 * 2 * 128 * 2 and l["kv_cache_per_token"] == 28 * 8 * 128 * 2
    assert q["params_b"] == 3.09 and l["params_b"] == 3.21


def test_read_design_uses_only_local_files(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(QCFG))
    d = read_design("any/model", finder=lambda m, n: tmp_path / n if (tmp_path / n).exists() else None)
    assert d["layers"] == 36 and "params_b" not in d                   # no index file: no size
    assert read_design("missing/model", finder=lambda m, n: None) is None


# --------------------------------------------------------------------------------- facts
def test_every_report_section_gets_measured_facts():
    res = load("synth-real-two")
    out = build_all(res, DESIGN, Insights(res))
    for q in ("How many flows can one GPU handle?", "How fast is each model?", "What does it cost per 1,000 flows?",
              "How much GPU memory does it need?", "Are the token budgets right?"):
        assert slug(q) in out["cards"], q
    for v in out["cards"].values():
        assert any(f["kind"] == "measured" for f in v["facts"])
        assert all(":" not in f["text"] and ";" not in f["text"] for f in v["facts"])
    assert set(out["why"]) == {"qwen2-5-7b", "llama-3-8b"}


def test_direction_rule_for_design_facts():
    res = load("synth-real-two")             # Qwen used less working memory than Llama
    out = build_all(res, DESIGN, Insights(res))
    fx = facts_of(out, "why", "qwen2-5-7b")
    assert any(t.startswith("design:Llama 3 8B likely needs more working memory") for t in fx)
    # swapped designs point the wrong way: only "would usually ..., but here ..." is offered
    swapped = build_all(res, {Q: DESIGN[L], L: DESIGN[Q]}, Insights(res))
    fs = facts_of(swapped, "why", "qwen2-5-7b")
    assert not any("likely needs more working memory" in t for t in fs)
    assert any("would usually need more cache per token" in t and "but here it used less" in t for t in fs)
    # the vocabulary: larger, but the token counts are within 2%, so it is "would usually ..., but"
    assert any("larger vocabulary would usually" in t and "about the same" in t for t in fx)


def test_no_design_facts_without_local_files():
    res = load("synth-real-two")
    out = build_all(res, {}, Insights(res))
    assert not any(f["kind"] == "design" for v in out["why"].values() for f in v["facts"])


# --------------------------------------------------------------------------------- checks
def _by(*facts):
    return {f["id"]: f for f in facts}


def test_card_sentence_checks():
    m = {"id": "c.1", "text": "Qwen2.5 7B averaged 10.80 s per flow.", "values": [10.8], "subjects": ["Qwen2.5 7B"], "kind": "measured"}
    d = {"id": "c.2", "text": "Qwen2.5 7B has 36 layers with 2 key-value heads each.", "values": [36, 2], "subjects": ["Qwen2.5 7B"], "kind": "design"}
    dg = {"session": {"models": ["Qwen2.5 7B"], "n_models": 1}}
    ok = explain.check_card_sentence
    assert ok("Qwen2.5 7B averaged 10.80 s per flow.", ["c.1"], _by(m, d), dg) is None
    assert "likely" in ok("Qwen2.5 7B has 36 layers with 2 key-value heads each.", ["c.2"], _by(m, d), dg)
    assert ok("Qwen2.5 7B likely has 36 layers with 2 key-value heads each.", ["c.2"], _by(m, d), dg) is None
    assert "first or second person" in ok("We saw Qwen2.5 7B average 10.80 s per flow.", ["c.1"], _by(m, d), dg)
    assert "stock phrase" in ok("Notably, Qwen2.5 7B averaged 10.80 s per flow.", ["c.1"], _by(m, d), dg)
    assert "not in its statements" in ok("Qwen2.5 7B averaged 11.80 s per flow.", ["c.1"], _by(m, d), dg)


# --------------------------------------------------------------------------------- explainer + PDF
@pytest.fixture(scope="module")
def explained(tmp_path_factory):
    import os
    os.environ["AGENTMETER_TEST_EXPLAINER"] = "1"
    try:
        res = load("synth-real-two")
        orig = explain.report_facts
        explain.report_facts = lambda r, design=None: orig(r, DESIGN)
        try:
            ex = explain.explain_session(res, tmp_path_factory.mktemp("ex"), real=True)
        finally:
            explain.report_facts = orig
    finally:
        os.environ.pop("AGENTMETER_TEST_EXPLAINER", None)
    return res, ex


def test_the_explainer_writes_checked_paragraphs(explained):
    _, ex = explained
    rep = ex["report"]
    assert len(rep["cards"]) >= 20 and set(rep["why"]) == {"qwen2-5-7b", "llama-3-8b"}
    for v in list(rep["cards"].values()) + list(rep["why"].values()):
        assert len(v["sentences"]) >= 2
    likely = [s for v in rep["why"].values() for s in v["sentences"] if s.get("likely")]
    assert likely and all(re.search(r"\blikely\b", s["text"]) for s in likely)
    assert rep["design"] and rep["why"]["qwen2-5-7b"]["model"] == "Qwen2.5 7B"


def test_pdf_shows_paragraphs_and_the_why_part(explained):
    pytest.importorskip("weasyprint")
    from agentmeter.session.report_html import Report
    res, ex = explained
    h = Report(res, explanation=ex).html()
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h[h.index("<body>"):])))
    assert "Why each model performed this way" in t and "The design facts, side by side" in t
    assert h.count('<p class="read">') >= 20 and 'class="likely"' in h and '<sup class="d">D1' in h
    para = ex["report"]["cards"][slug("How many flows can one GPU handle?")]["sentences"][0]["text"]
    assert para in t
    # without an explanation: no why part, and the sections keep their one-line answers
    h0 = Report(res).html()
    assert "Why each model performed this way" not in h0 and '<p class="read">' not in h0 and '<p class="ans">' in h0
