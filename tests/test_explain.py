"""The session explainer, checked (agentmeter/session/explain.py, statements.py, answer_patterns.py;
design: docs/superpowers/specs/2026-11-11-checked-explanations-design.md)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmeter.session import answer_patterns as P
from agentmeter.session import explain as E
from agentmeter.session import statements as S

FIX = Path(__file__).parent / "fixtures" / "golden_session" / "golden_session_results.json"
MISTRAL = "mistralai/Mistral-7B-Instruct-v0.3"


@pytest.fixture()
def res():
    return json.loads(FIX.read_text(encoding="utf-8"))


@pytest.fixture()
def digest(res):
    return E.build_digest(res, None, E.load_config())


def one_model():
    """A one-model real session like the RTX 3090 trial (Mistral 7B, 11 flows)."""
    return {
        "session": {"n_flows": 11, "run_id": "r1", "accuracy_available": False},
        "environment": {"provider": "real", "gpu_name": "NVIDIA GeForce RTX 3090"},
        "per_model": [{"model": MISTRAL, "efficiency": {"end_to_end": {"mean_latency_s": 16.19, "p95_latency_s": 19.97,
                                                                       "mean_tokens_per_flow": 4158}},
                       "saw_efficiency_only": {"composite_100": 66.3, "tier": "Degraded"}}],
        "phase8": {"targets": {"latency_s": 7.9, "vram_mb": 300, "tokens_total": 3361},
                   "tiers": {"healthy_min": 80, "degraded_min": 60},
                   "saw_table": [{"model": MISTRAL, "latency_s": 16.19, "vram_mb": 410, "tokens_total": 4158}],
                   "normalised": [{"model": MISTRAL, "latency": 0.49, "vram": 0.77, "tokens": 0.81}]},
        "phase8_efficiency_only": {"weights": {"latency": 0.25, "vram": 0.2, "tokens": 0.15}},
        "memory": {"per_model": [{"model": MISTRAL, "total_peak_mb": 5351}]},
        "cost_energy": {"per_model": [{"model": MISTRAL, "cost_per_1k_flows_usd": 1.0365}]},
        "output_reliability": {"label_agent": "decide", "per_model": [{"model": MISTRAL, "valid_rate": 1.0}]},
        "agents": {"agent_order": ["perceive", "reason", "decide", "act"], "per_model": [{"model": MISTRAL, "n_flows": 11, "agents": [
            {"agent": "perceive", "n": 11, "latency_share": 0.37, "token_share": 0.30, "mean_latency_s": 6.21,
             "mean_output_tokens": 155, "output_tokens_max": 160, "failures": {"token_cap": 160, "hit_token_cap": 9}},
            {"agent": "decide", "n": 11, "latency_share": 0.03, "token_share": 0.10, "mean_latency_s": 0.5,
             "mean_output_tokens": 2, "output_tokens_max": 3, "failures": {"token_cap": 12, "hit_token_cap": 0}}]}]},
        "per_agent": {"table": [{"model": MISTRAL, "agent_name": "perceive", "mean_wall_s": 6.21, "mean_ttft_s": 0.33,
                                 "mean_output_tokens": 155, "mean_input_tokens": 922, "mean_vram_delta_mb": 288},
                                {"model": MISTRAL, "agent_name": "decide", "mean_wall_s": 0.5, "mean_ttft_s": 0.36,
                                 "mean_output_tokens": 2, "mean_input_tokens": 1130, "mean_vram_delta_mb": 324}]},
    }


@pytest.fixture()
def sess():
    r = one_model()
    d = E.build_digest(r, None, E.load_config())
    return r, d, S.build_statements(r, d)


# ---------------------------------------------------------------------------- names and digest
def test_everyday_names_match_the_app():
    assert E.nick(MISTRAL) == "Mistral 7B"
    assert E.nick("meta-llama/Meta-Llama-3-8B-Instruct") == "Llama 3 8B"
    assert E.nick("mock:google/gemma-2-9b-it") == "Gemma 2 9B"
    assert E.nick("microsoft/Phi-3-mini-4k-instruct") == "Phi-3 mini"


def test_digest_uses_everyday_names_and_budget_verdicts(sess):
    _, d, _ = sess
    s = json.dumps(d)
    assert "mistralai/" not in s and d["session"]["comparison"].startswith("only one model ran")
    pm = d["per_model"][0]
    assert pm["slowest_step"]["step"] == "Perceive"
    assert pm["most_cut_off_step"]["budget_advice"] == "raise the budget to about 200 tokens, 9 of 11 answers were cut off at 160 tokens"
    assert next(x for x in pm["steps"] if x["step"] == "Decide")["budget_advice"].startswith("the budget could be lowered")


# ---------------------------------------------------------------------------- statements
def test_statements_read_like_the_page(sess):
    _, _, st = sess
    dep = {x["text"] for x in st["deployment"]}
    assert "Only one model ran, Mistral 7B, so this is a reading of one model, not a choice between models." in dep
    assert "At 16.19 s per flow, one GPU answers about 5,337 flows a day, one at a time." in dep
    assert "Mistral 7B needed 5.2 GB of GPU memory at its peak, so a GPU with 8 GB or more leaves room for longer prompts." in dep
    assert "9 of 11 Perceive answers from Mistral 7B were cut off at 160 tokens." in dep
    agents = {x["text"]: x for x in st["agents"]}
    t = agents["Perceive takes 37% of each flow's time for Mistral 7B, 6.21 s per flow."]
    assert t["values"] == [37.0, 6.21] and t["subjects"] == ["Mistral 7B", "Perceive"]
    assert "Perceive uses 30% of all tokens for Mistral 7B." in agents
    assert not any("ranks first" in x["text"] for x in st["ranking"])             # one model: nothing ranked
    assert all(":" not in x["text"] and ";" not in x["text"] for v in st.values() for x in v)


def test_a_criterion_that_was_not_measured_is_missing_not_lost(sess):
    r, _, _ = sess
    r["phase8"]["saw_table"][0]["vram_mb"] = None
    st = S.build_statements(r, E.build_digest(r, None, E.load_config()))
    texts = [x["text"] for x in st["deployment"]]
    assert any(t.startswith("GPU memory was not measured in this run") for t in texts)
    assert not any("lost on GPU memory" in t for t in texts)


# ---------------------------------------------------------------------------- sentences
def test_a_sentence_is_checked_against_its_own_statements(sess):
    _, d, st = sess
    by = {x["id"]: x for x in st["agents"]}
    time_id = next(i for i, x in by.items() if x["text"].startswith("Perceive takes 37%"))
    tok_id = next(i for i, x in by.items() if x["text"].startswith("Perceive uses 30%"))
    ok = "Perceive takes 37% of each flow's time for Mistral 7B, more than any other step."
    assert E.check_sentence(ok, [time_id], by, d) is None
    assert "30 is not in its statements" in E.check_sentence("Perceive also uses 30% of all tokens.", [time_id], by, d)
    assert E.check_sentence("Perceive also uses 30% of all tokens.", [tok_id], by, d) is None
    assert "names Decide" in E.check_sentence("Decide takes 37% of each flow's time.", [time_id], by, d)
    assert "not in this section" in E.check_sentence(ok, ["dep.1"], by, d)
    assert "names no statement" in E.check_sentence(ok, [], by, d)


@pytest.mark.parametrize("text,why", [
    ("Perceive takes 37% of the time; it is the slowest.", "semicolon"),
    ("Note, Perceive takes 37% of the time and that is reliable overall.", "reassurance"),
    ("Mistral 7B is the most efficient model, with Perceive at 37% of the time.", "one-model"),
    ("Increase the Perceive budget to make the pipeline faster, as Perceive takes 37% of the time.", "improves efficiency"),
    ("The 'perceive' step takes 37% of each flow's time.", "quote marks"),
])
def test_the_rules_still_apply_to_every_sentence(sess, text, why):
    _, d, st = sess
    by = {x["id"]: x for x in st["agents"]}
    tid = next(i for i, x in by.items() if x["text"].startswith("Perceive takes 37%"))
    assert why in (E.check_sentence(text, [tid], by, d) or "")


def test_cut_off_is_not_cutting_the_budget(sess):
    _, d, _ = sess
    assert E._budget_contradiction("9 of 11 Perceive answers were cut off at the 160 token budget.", d) is None
    assert "smaller budget" in E._budget_contradiction("Reduce the token budget of Perceive.", d)


# ---------------------------------------------------------------------------- requests, retries, fallbacks
def _gen_from(replies):
    calls = []

    def gen(msgs):
        user = msgs[-1]["content"]
        req = json.loads(user.split("INPUT\n", 1)[1].split("\n\n", 1)[0])
        key = req.get("section") or req.get("group")
        calls.append((key, "earlier" in user))
        r = replies(req, "earlier" in user)
        return (r if isinstance(r, str) else json.dumps(r)), 10, 5
    return gen, calls


def test_one_request_per_section_with_a_retry_and_a_fallback(sess):
    _, d, st = sess

    def replies(req, again):
        sec, ss = req["section"], req["statements"]
        if sec == "tab.agents" and not again:
            return {"sentences": [{"text": "Perceive uses 99% of all tokens.", "uses": [ss[0]["id"]]}]}     # a number no statement has
        if sec == "tab.cost":
            return "I think the cost is fine."                                                             # never JSON: fallback
        return {"sentences": [{"text": s["text"], "uses": [s["id"]]} for s in ss[:2]]}

    gen, calls = _gen_from(replies)
    out = E.orchestrate(gen, st, [], d, E.load_config())
    assert ("tab.agents", True) in calls                                    # asked once more, with the problem
    assert out["sections"]["tab.agents.h"].startswith("Perceive takes 37%")
    assert "tab.cost" in out["dropped"] and "tab.cost.h" not in out["sections"]
    assert out["status"] == "partial" and out["tokens"]["input"] > 0
    assert [c for c in calls if c[0] == "tab.cost"] == [("tab.cost", False), ("tab.cost", True)]


def test_a_tab_needs_its_first_sentence_as_a_full_headline(sess):
    _, d, st = sess

    def replies(req, again):
        ss = req["statements"]
        return {"sentences": [{"text": "Perceive.", "uses": [ss[0]["id"]]}, {"text": ss[1]["text"], "uses": [ss[1]["id"]]}]}

    out = E.orchestrate(_gen_from(replies)[0], {"agents": st["agents"]}, [], d, E.load_config())
    assert "tab.agents" in out["dropped"]


def test_repeated_sentences_are_dropped_across_sections(sess):
    _, d, st = sess
    same = st["deployment"][0]

    def replies(req, again):
        return {"sentences": [{"text": same["text"], "uses": [s["id"] for s in req["statements"] if s["text"] == same["text"]]}] * 2}

    st2 = {"recommendations": [same, *st["deployment"][1:2]], "deployment": st["deployment"]}
    out = E.orchestrate(_gen_from(replies)[0], st2, [], d, E.load_config())
    assert "deployment" in out["dropped"] and "repeats" in out["dropped"]["deployment"]


def test_replies_in_fences_or_cut_off_are_still_read():
    assert E._json_reply('```json\n{"sentences": []}\n```') == {"sentences": []}
    cut = '{"sentences": [{"text": "Perceive takes 37% of each flow\'s time.", "uses": ["agen.1"]}, {"text": "It also'
    assert E._json_reply(cut) == {"sentences": [{"text": "Perceive takes 37% of each flow's time.", "uses": ["agen.1"]}]}
    with pytest.raises(ValueError):
        E._json_reply("I cannot help with that.")


# ---------------------------------------------------------------------------- answer patterns
def _flows(texts_cut, texts_ok):
    flows = []
    for i, t in enumerate(texts_cut):
        flows.append({"truth": "Benign", "per_model": {MISTRAL: {"correct": True, "predicted": "Benign", "agents": [
            {"agent": "perceive", "text": t, "output_tokens": 160}]}}})
    for t in texts_ok:
        flows.append({"truth": "Benign", "per_model": {MISTRAL: {"correct": True, "predicted": "Benign", "agents": [
            {"agent": "perceive", "text": t, "output_tokens": 60}]}}})
    return {"has_text": True, "agent_order": ["perceive", "reason", "decide", "act"], "flows": flows}


CUT = ["Flow duration 47 us, Total Fwd Packets 1, Total Backward Packets 0, Fwd Packet Length Max 0 from 10.0.0.5 port 4444",
       "Flow duration 12 us, Total Fwd Packets 2, Total Backward Packets 0, Fwd Packet Length Max 6",
       "Flow duration 90 us, Total Fwd Packets 1, Total Backward Packets 1, Fwd Packet Length",
       "Summary of the flow follows after the list of features"]
OK = ["Summary: a short burst to one port, consistent with scanning.", "Summary: a single packet, likely benign.",
      "Summary: steady traffic, typical web browsing."]


@pytest.fixture()
def group(sess):
    r, _, _ = sess
    g = P.build_groups(r, _flows(CUT, OK), {MISTRAL: "Mistral 7B"}, E.load_config())
    assert len(g) == 1
    return g[0]


def test_groups_are_counted_by_code_and_scrubbed(group):
    assert group["kind"] == "cut_off" and group["step"] == "Perceive" and group["n"] == 4 and group["budget"] == 160
    assert group["describe"] == "cut-off Perceive answers" and len(group["contrast"]) == 3
    assert not any("10.0.0.5" in t or "port 4444" in t for t in group["texts"])


def test_a_supported_pattern_is_kept_with_codes_count(group):
    kept, why = P.check_pattern({"pattern": "Cut-off Perceive answers list every flow feature one by one before any summary.",
                                 "key_phrase": "Total Fwd Packets", "quote": "Flow duration 12 us, Total Fwd Packets 2"}, group)
    assert why is None and kept["found_in"] == 3 and kept["of"] == 4
    assert kept["label"] == "Found in 3 of 4 cut-off Perceive answers."


@pytest.mark.parametrize("reply,why", [
    ({"pattern": "They list features.", "key_phrase": "Total Fwd Packets", "quote": "Total Fwd Packets 7 and more text"}, "word for word"),
    ({"pattern": "They list features.", "key_phrase": "the list of features", "quote": "Summary of the flow follows after"}, "only 1 of 4"),
    ({"pattern": "They list features.", "key_phrase": "Flow duration 47", "quote": "Flow duration 47 us, Total"}, "only 1 of 4"),
    ({"pattern": "They list 3 features.", "key_phrase": "Total Fwd Packets", "quote": "Flow duration 47 us, Total"}, "number"),
    ({"pattern": "They list features; then stop.", "key_phrase": "Total Fwd Packets", "quote": "Flow duration 47 us, Total"}, "semicolon"),
])
def test_unsupported_patterns_are_dropped(group, reply, why):
    kept, reason = P.check_pattern(reply, group)
    assert kept is None and why in reason


def test_a_phrase_as_common_in_the_other_answers_explains_nothing(group):
    g = dict(group, contrast=["Total Fwd Packets 3 then a summary", "Total Fwd Packets 1 and done", "Summary only"])
    kept, why = P.check_pattern({"pattern": "They list features.", "key_phrase": "Total Fwd Packets",
                                 "quote": "Flow duration 12 us, Total Fwd Packets 2"}, g)
    assert kept is None and "as common in the other answers" in why


def test_a_mix_up_phrase_must_be_more_than_the_class_names():
    g = {"kind": "mix_up", "true": "Port Scanning", "answered": "Benign", "texts": ["Classification Benign now"] * 3, "contrast": []}
    kept, why = P.check_pattern({"pattern": "They say Benign.", "key_phrase": "Classification Benign", "quote": "Classification Benign now please"}, g)
    assert kept is None


# ---------------------------------------------------------------------------- the whole step
def test_demo_run_writes_nothing_but_a_skipped_record(tmp_path, res, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_EXPLAINER", raising=False)
    out = E.explain_session(res, tmp_path, real=False)
    assert out["status"] == "skipped" and "demo" in out["reason"]


def test_no_gpu_room_is_skipped_with_the_reason(tmp_path, res, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_EXPLAINER", raising=False)
    out = E.explain_session(res, tmp_path, real=True, free_vram_gb=4.0)
    assert out["status"] == "skipped" and "memory" in out["reason"]


def test_a_failed_model_gives_way_to_the_next(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_EXPLAINER", raising=False)
    tried = []

    def runner(mspec, msgs, cfg):
        tried.append(mspec["id"])
        if mspec["id"].startswith("swiss-ai"):
            e = E.ExplainerError("explainer process failed: OutOfMemoryError: CUDA out of memory")
            e.trace = "torch.OutOfMemoryError: CUDA out of memory"
            raise e
        req = json.loads(msgs[-1]["content"].split("INPUT\n", 1)[1].split("\n\n", 1)[0])
        ss = req.get("statements") or []
        return {"text": json.dumps({"sentences": [{"text": s["text"], "uses": [s["id"]]} for s in ss[:2]]}), "tokens": {}}

    out = E.explain_session(one_model(), tmp_path, real=True, runner=runner, free_vram_gb=23.0)
    assert out["model"] == "openai/gpt-oss-20b" and out["attempts"][0]["model"].startswith("swiss-ai")
    assert "out of memory" in out["attempts"][0]["reason"] and out["sections"]


def test_the_test_explainer_drops_its_wrong_tag_and_made_up_quote(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMETER_TEST_EXPLAINER", "1")
    r = one_model()
    (tmp_path / "flows_r1.json").write_text(json.dumps(_flows(CUT, OK)))
    out = E.explain_session(r, tmp_path, real=False)
    assert out["model"] == "test-explainer" and out["status"] in ("ok", "partial")
    assert any("not in its statements" in x["why"] for x in out["detail"]["tab.agents"]["dropped"])
    assert out["patterns"] and out["patterns"][0]["label"].startswith("Found in")
    saved = json.loads((tmp_path / "explanation.json").read_text())
    assert saved["statements"] and saved["detail"] and saved["statements_sha256"]


def test_explainer_models_downloaded_at_rental_start():
    cfg = E.load_config()
    assert E.startup_models(24.0, cfg) == ["swiss-ai/Apertus-8B-Instruct-2509", "openai/gpt-oss-20b"]
    assert E.startup_models(12.0, cfg) == []
    assert "openai/gpt-oss-20b" not in Path(E.ROOT / "configs" / "model_catalog.yaml").read_text()


# ---------------------------------------------------------------------------- the model's process
class _T:
    def __init__(self, ids): self.ids = list(ids)
    @property
    def shape(self): return (1, len(self.ids))
    def to(self, _d): return self


class _Row(list):
    @property
    def shape(self): return (len(self),)
    def __getitem__(self, s): return _Row(list.__getitem__(self, s)) if isinstance(s, slice) else list.__getitem__(self, s)


def _fake_modules(monkeypatch, *, as_mapping):
    import contextlib
    import sys
    import types
    torch = types.ModuleType("torch")
    torch.no_grad = contextlib.nullcontext
    tr = types.ModuleType("transformers")
    seen = {"loads": 0}

    class Tok:
        @classmethod
        def from_pretrained(cls, mid): return cls()
        def apply_chat_template(self, msgs, **kw):
            seen["kw"] = kw
            ids = _T([1, 2, 3])
            return {"input_ids": ids, "attention_mask": _T([1, 1, 1])} if as_mapping else ids
        def decode(self, toks, skip_special_tokens=False): return '{"sentences": []}'

    class Model:
        device = "cuda"
        @classmethod
        def from_pretrained(cls, mid, **kw):
            seen["loads"] += 1
            seen["load_kw"] = kw
            return cls()
        def generate(self, **kw):
            seen["gen_kw"] = kw
            return [_Row([1, 2, 3, 9, 9])]

    tr.AutoTokenizer, tr.AutoModelForCausalLM = Tok, Model
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", tr)
    return seen


@pytest.mark.parametrize("as_mapping", [True, False])
def test_the_model_loads_once_and_answers_many_requests(monkeypatch, as_mapping):
    seen = _fake_modules(monkeypatch, as_mapping=as_mapping)
    gen, _ = E._load_generator({"engine": "hf", "model": "m", "reasoning": "low", "max_new_tokens": 50})
    for _ in range(3):
        assert gen([{"role": "user", "content": "hi"}]) == ('{"sentences": []}', 3, 2)
    assert seen["loads"] == 1 and seen["load_kw"]["dtype"] == "auto"
    assert seen["kw"]["return_dict"] is True and seen["kw"]["reasoning_effort"] == "low"


def test_worker_reports_its_error_with_the_traceback(tmp_path, monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "transformers", types.ModuleType("transformers"))
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    (tmp_path / "in.json").write_text(json.dumps({"engine": "hf", "model": "m", "max_new_tokens": 5,
                                                  "plan": {"statements": {}, "groups": [], "digest": {}, "cfg": {}}}))
    assert E._worker_main([str(tmp_path / "in.json"), str(tmp_path / "out.json")]) == 1
    got = json.loads((tmp_path / "out.json").read_text())
    assert "ImportError" in got["error"] and "Traceback" in got["trace"]


def test_a_tie_follows_the_page_order():
    r = one_model()
    r["per_model"].append({"model": "meta-llama/Meta-Llama-3-8B-Instruct", "efficiency": {"end_to_end": {"mean_latency_s": 16.19}},
                           "saw_efficiency_only": {"composite_100": 66.3, "tier": "Degraded", "rank": 1}})
    r["per_model"][0]["saw_efficiency_only"]["rank"] = 2
    d = E.build_digest(r, None, E.load_config())
    st = S.build_statements(r, d)
    assert st["ranking"][0]["text"].startswith("Llama 3 8B scores 66.3")
    assert not any("Mistral 7B" in x["text"] for x in st["deployment"][:1])
