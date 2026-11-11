"""The session explainer (agentmeter/session/explain.py): digest, the owner's checks, the
fallback for a failed section, and that it never fails a session."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmeter.session import explain as E

FIX = Path(__file__).parent / "fixtures" / "golden_session" / "golden_session_results.json"


@pytest.fixture()
def res():
    return json.loads(FIX.read_text(encoding="utf-8"))


@pytest.fixture()
def digest(res):
    return E.build_digest(res, None, E.load_config())


def test_everyday_names_match_the_app():
    assert E.nick("mistralai/Mistral-7B-Instruct-v0.3") == "Mistral 7B"
    assert E.nick("meta-llama/Meta-Llama-3-8B-Instruct") == "Llama 3 8B"
    assert E.nick("mock:google/gemma-2-9b-it") == "Gemma 2 9B"
    assert E.nick("microsoft/Phi-3-mini-4k-instruct") == "Phi-3 mini"


def test_digest_is_this_session_only_and_uses_everyday_names(digest):
    s = json.dumps(digest)
    assert digest["session"]["models"] == ["Qwen2.5 7B", "Phi-3 mini"]
    assert "Qwen/" not in s and "mock:" not in s
    assert digest["per_model"][0]["name"] == "Qwen2.5 7B"
    assert digest["per_model"][0]["efficiency_score"] == 62.6


def test_digest_excerpts_drop_addresses_and_ports(tmp_path, res):
    run_id = res["session"]["run_id"]
    flows = {"flows": [{"scenario_id": "r1", "truth": "Port Scanning", "per_model": {
        res["per_model"][0]["model"]: {"predicted": "Benign", "correct": False, "latency_s": 2.5, "agents": [
            {"agent": "decide", "wall_s": 1.0, "input_tokens": 400, "output_tokens": 3,
             "text": "Flow 10.0.0.5 to port 4444 looks Benign"},
            {"agent": "act", "wall_s": 0.5, "input_tokens": 50, "output_tokens": 0, "text": ""}]}}}]}
    (tmp_path / f"flows_{run_id}.json").write_text(json.dumps(flows), encoding="utf-8")
    d = E.build_digest(res, tmp_path, E.load_config())
    s = json.dumps(d)
    assert "10.0.0.5" not in s and "4444" not in s and "[removed]" in s
    probs = {x["problem"] for x in d["answer_excerpts"]}
    assert probs == {"empty answer", "mix-up"}
    assert d["slowest_flows"][0]["time_s"] == 2.5


@pytest.mark.parametrize("text,why", [
    ("Qwen2.5 7B is faster; it wins.", "semicolon"),
    ("Note: Qwen2.5 7B is faster.", "colon"),
    ("Qwen2.5 7B is faster — by a lot.", "dash"),
    ("Qwen2.5 7B is faster – by a lot.", "dash"),
    ("Qwen2.5 7B is faster - by a lot.", "dash"),
    ("Qwen/Qwen2.5-7B-Instruct is faster.", "full model id"),
])
def test_style_rule_rejects_each_symbol(text, why, digest):
    assert why in (E.check_section("tab.performance.h", text, digest) or "")


def test_hyphens_inside_words_and_counts_are_fine(digest):
    ok = "Phi-3 mini runs at 4-bit like every per-agent step, and 3 runs would settle it."
    assert E.check_section("summary", ok, digest) is None


def test_numbers_must_come_from_the_results(digest):
    tok = digest["per_model"][0]["tokens_per_flow"]
    assert E.check_section("summary", f"Qwen2.5 7B uses {int(tok):,} tokens per flow.", digest) is None
    assert "not in the results" in E.check_section("summary", "Qwen2.5 7B uses 98,765 tokens per flow.", digest)


def test_word_limit(digest):
    assert "too long" in E.check_section("tab.ranking.h", " ".join(["word"] * 40), digest)


def test_demo_run_writes_nothing_but_a_skipped_record(tmp_path, res, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_EXPLAINER", raising=False)
    out = E.explain_session(res, tmp_path, real=False)
    assert out["status"] == "skipped" and "demo" in out["reason"]
    assert json.loads((tmp_path / "explanation.json").read_text())["status"] == "skipped"


def test_no_gpu_room_is_skipped_with_the_reason(tmp_path, res, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_EXPLAINER", raising=False)
    out = E.explain_session(res, tmp_path, real=True, free_vram_gb=4.0)
    assert out["status"] == "skipped" and "memory" in out["reason"]


def test_fake_explainer_retries_and_falls_back_per_section(tmp_path, res, monkeypatch):
    monkeypatch.setenv("AGENTMETER_TEST_EXPLAINER", "1")
    out = E.explain_session(res, tmp_path, real=False)
    assert out["status"] == "partial"
    assert "tab.cost.h" in out["sections"] and ";" not in out["sections"]["tab.cost.h"]   # fixed on the retry
    assert out["dropped"] == {"tab.agents.h": "uses a colon"}                            # still broken: standard text
    assert out["model"] == "test-explainer" and out["prompt_sha256"] and out["digest_sha256"]


def test_a_model_reply_is_checked_and_a_failure_never_raises(tmp_path, res, monkeypatch):
    monkeypatch.delenv("AGENTMETER_TEST_EXPLAINER", raising=False)
    calls = []

    def runner(mspec, messages, cfg):
        calls.append(messages)
        body = {"summary": "Qwen2.5 7B leads.", "tab.ranking.h": "It scored 999.9 points."}
        return {"text": "<|channel|>final<|message|>" + json.dumps(body), "tokens": {"input": 10, "output": 5}}

    out = E.explain_session(res, tmp_path, real=True, runner=runner, free_vram_gb=40.0)
    assert out["sections"] == {"summary": "Qwen2.5 7B leads."}
    assert "not in the results" in out["dropped"]["tab.ranking.h"]
    assert len(calls) == 2 and "broke a rule" in calls[1][1]["content"]                  # one retry
    assert out["model"] == "openai/gpt-oss-20b"

    def broken(mspec, messages, cfg):
        raise RuntimeError("CUDA out of memory")

    out = E.explain_session(res, tmp_path, real=True, runner=broken, free_vram_gb=40.0)
    assert out["status"] == "failed" and "CUDA out of memory" in out["reason"]


def test_one_model_downloaded_at_rental_start():
    cfg = E.load_config()
    assert E.startup_model(24.0, cfg) == "openai/gpt-oss-20b"
    assert E.startup_model(12.0, cfg) is None
    assert E.startup_model(None, cfg) == "openai/gpt-oss-20b"
    assert "openai/gpt-oss-20b" not in Path(E.ROOT / "configs" / "model_catalog.yaml").read_text()


# --- the transformers path, with stand-in torch/transformers modules (no GPU in tests) ---------
class _T:
    """A tiny tensor: a list of token ids with the few methods the explainer uses."""
    def __init__(self, ids): self.ids = list(ids)
    @property
    def shape(self): return (1, len(self.ids))
    def to(self, _device): return self
    def __getitem__(self, i): return self if i == 0 else None


class _Gen(list):
    def __getitem__(self, i):
        return _Row(list.__getitem__(self, i)) if isinstance(i, int) else list.__getitem__(self, i)


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
    seen = {}

    class Tok:
        @classmethod
        def from_pretrained(cls, mid): return cls()
        def apply_chat_template(self, msgs, **kw):
            seen["kw"] = kw
            ids = _T([1, 2, 3])
            return {"input_ids": ids, "attention_mask": _T([1, 1, 1])} if as_mapping else ids
        def decode(self, toks, skip_special_tokens=False):
            return '<|channel|>final<|message|>{"summary": "Short."}<|return|>'

    class Model:
        device = "cuda"
        @classmethod
        def from_pretrained(cls, mid, **kw):
            seen["load_kw"] = kw
            return cls()
        def generate(self, **kw):
            seen["gen_kw"] = kw
            return _Gen([[1, 2, 3, 9, 9]])

    tr.AutoTokenizer, tr.AutoModelForCausalLM = Tok, Model
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", tr)
    return seen


@pytest.mark.parametrize("as_mapping", [True, False])
def test_transformers_path_handles_v5_mappings_and_bare_tensors(tmp_path, monkeypatch, as_mapping):
    seen = _fake_modules(monkeypatch, as_mapping=as_mapping)
    spec = {"engine": "hf", "model": "openai/gpt-oss-20b", "reasoning": "low", "max_new_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    (tmp_path / "in.json").write_text(json.dumps(spec))
    assert E._worker_main([str(tmp_path / "in.json"), str(tmp_path / "out.json")]) == 0
    got = json.loads((tmp_path / "out.json").read_text())
    assert got["tokens"] == {"input": 3, "output": 2}
    assert E._json_reply(got["text"]) == {"summary": "Short."}
    assert seen["load_kw"]["dtype"] == "auto" and "torch_dtype" not in seen["load_kw"]
    assert seen["kw"]["return_dict"] is True and seen["kw"]["reasoning_effort"] == "low"
    assert "input_ids" in seen["gen_kw"]


def test_worker_reports_its_error_with_the_traceback(tmp_path, monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "transformers", types.ModuleType("transformers"))   # no Auto* classes
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    (tmp_path / "in.json").write_text(json.dumps({"engine": "hf", "model": "m", "max_new_tokens": 5, "messages": []}))
    assert E._worker_main([str(tmp_path / "in.json"), str(tmp_path / "out.json")]) == 1
    got = json.loads((tmp_path / "out.json").read_text())
    assert "ImportError" in got["error"] and "Traceback" in got["trace"]
