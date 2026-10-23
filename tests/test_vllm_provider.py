"""vLLM engine (providers/vllm_provider.py): 4-bit NF4 only, open models, and the batching
dispatcher — against a fake `vllm` module (no GPU, no vLLM install needed)."""
from __future__ import annotations

import copy
import sys
import threading
import time
import types

import pytest
import yaml

from agentmeter.config import PROJECT_ROOT, Config, load_config
from agentmeter.providers import get_provider
from agentmeter.providers.vllm_provider import (QUANTIZATION, VLLMConfigError, VLLMProvider,
                                                check_quantization)

L4 = PROJECT_ROOT / "configs" / "run_full_l4.yaml"


class FakeTok:
    chat_template = "{fake}"

    def __init__(self, reject_system=False):
        self.reject_system = reject_system

    def apply_chat_template(self, messages, add_generation_prompt=True, tokenize=False):
        if self.reject_system and any(m["role"] == "system" for m in messages):
            raise ValueError("System role not supported")
        return "".join(f"<{m['role']}>{m['content']}" for m in messages) + "<assistant>"


class FakeLLM:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.batches: list[int] = []
        self.tok = FakeTok(reject_system=kw["model"].endswith("nosys"))
        FakeLLM.instances.append(self)

    def get_tokenizer(self):
        return self.tok

    def generate(self, prompts, params, use_tqdm=True):
        assert use_tqdm is False and len(prompts) == len(params)
        self.batches.append(len(prompts))
        time.sleep(0.05)
        out = []
        for p, sp in zip(prompts, params):
            text = f"echo:{p}"
            out.append(types.SimpleNamespace(prompt_token_ids=list(range(len(p.split()))),
                                             outputs=[types.SimpleNamespace(text=" " + text + " ", token_ids=[1] * min(sp.max_tokens, 3))]))
        return out


class FakeSamplingParams:
    def __init__(self, max_tokens, temperature, seed):
        self.max_tokens, self.temperature, self.seed = max_tokens, temperature, seed


@pytest.fixture
def fake_vllm(monkeypatch):
    mod = types.ModuleType("vllm")
    mod.LLM, mod.SamplingParams = FakeLLM, FakeSamplingParams
    monkeypatch.setitem(sys.modules, "vllm", mod)
    FakeLLM.instances.clear()
    return mod


def cfg(**changes):
    base = load_config(L4)
    data = copy.deepcopy(base.data)
    data["model"]["engine"] = "vllm"
    data["model"]["name"] = changes.pop("name", "Qwen/Qwen2.5-7B-Instruct")
    q = data["model"]["hf"]["quantization"]
    q.update(changes.pop("quant", {}))
    if changes.get("vllm"):
        data["model"]["vllm"] = changes.pop("vllm")
    return Config(data, base.path)


# ---------------------------------------------------------------- 4-bit NF4 only
def test_the_study_config_is_4bit_nf4_and_accepted():
    check_quantization(cfg())


@pytest.mark.parametrize("quant,vllm,msg", [
    ({"load_in_4bit": False}, None, "load_in_4bit must be true"),
    ({"load_in_8bit": True}, None, "8-bit is not allowed"),
    ({"bnb_4bit_quant_type": "fp4"}, None, "must be nf4"),
    ({"bnb_4bit_use_double_quant": False}, None, "double_quant must be true"),
    ({}, {"quantization": "awq"}, "must be 'bitsandbytes'"),
    ({}, {"quantization": "fp8"}, "must be 'bitsandbytes'"),
])
def test_anything_but_4bit_nf4_is_refused(quant, vllm, msg):
    with pytest.raises(VLLMConfigError, match=msg):
        VLLMProvider(cfg(quant=quant, vllm=vllm))


def test_get_provider_picks_vllm_only_when_asked(fake_vllm):
    assert isinstance(get_provider(cfg()), VLLMProvider)
    demo = cfg()
    demo.data["model"]["provider"] = "mock"                 # the demo never runs an engine
    assert type(get_provider(demo)).__name__ == "MockProvider"


def test_engine_arguments_are_in_flight_nf4_and_no_remote_code(fake_vllm):
    p = VLLMProvider(cfg(vllm={"gpu_memory_utilization": 0.8, "max_model_len": 4096}))
    p.load()
    try:
        kw = FakeLLM.instances[-1].kw
        assert kw["quantization"] == QUANTIZATION == "bitsandbytes" and kw["load_format"] == "bitsandbytes"
        assert kw["trust_remote_code"] is False and kw["tensor_parallel_size"] == 1
        assert kw["gpu_memory_utilization"] == 0.8 and kw["max_model_len"] == 4096 and kw["dtype"] == "float16"
        assert p.measures_call_vram is False and p.quantization_label == "4bit-nf4"
    finally:
        p.unload()
    with pytest.raises(VLLMConfigError, match="gpu_memory_utilization"):
        VLLMProvider(cfg(vllm={"gpu_memory_utilization": 0.99}))


# ---------------------------------------------------------------- generation
def test_generate_applies_the_chat_template_and_counts_tokens(fake_vllm):
    p = VLLMProvider(cfg())
    p.load()
    try:
        r = p.generate("flow features here", system="You are the Decide agent.", max_new_tokens=16)
        assert r.text.startswith("echo:") and r.text == r.text.strip()
        assert r.output_tokens == 3 and r.input_tokens > 0 and r.ttft_s is None
        assert FakeLLM.instances[-1].batches == [1]          # a lone call: a batch of one
    finally:
        p.unload()


def test_a_template_without_a_system_role_gets_it_folded_in(fake_vllm):
    p = VLLMProvider(cfg(name="org/model-nosys"))
    p.load()
    try:
        assert p._prompt("hi", "be brief") == "<user>be brief\n\nhi<assistant>"
    finally:
        p.unload()


def test_concurrent_calls_are_batched_and_each_gets_its_own_answer(fake_vllm):
    p = VLLMProvider(cfg())
    p.load()
    try:
        res = {}

        def call(i):
            res[i] = p.generate(f"flow number {i:03d}", max_new_tokens=8).text
        ts = [threading.Thread(target=call, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert sorted(res) == list(range(6))
        assert all(res[i].endswith(f"{i:03d}<assistant>") for i in range(6))
        b = FakeLLM.instances[-1].batches
        assert sum(b) == 6 and max(b) > 1                   # requests in flight shared a batch
    finally:
        p.unload()


def test_engine_errors_reach_the_caller(fake_vllm):
    p = VLLMProvider(cfg())
    p.load()
    try:
        def boom(*a, **k):
            raise RuntimeError("CUDA out of memory")
        FakeLLM.instances[-1].generate = boom
        with pytest.raises(RuntimeError, match="out of memory"):
            p.generate("x")
    finally:
        p.unload()
    with pytest.raises(RuntimeError, match="not loaded"):
        p.generate("x")


def test_per_call_vram_is_not_recorded_for_vllm():
    from agentmeter.pipeline.instrument import MetricsCollector, make_instrumented_hook

    class Probe:
        def reset(self):
            pass

        def read_peak_delta_mb(self):
            return 123.0

    class Prov:
        measures_call_vram = False
    col = MetricsCollector()
    hook = make_instrumented_hook(col, "m", Probe())
    hook("decide", lambda s, p, c: {"_last_result": types.SimpleNamespace(ttft_s=None, input_tokens=1, output_tokens=1)},
         {"scenario_id": "s"}, Prov(), None)
    assert col.rows[0].vram_peak_mb is None
    hook("decide", lambda s, p, c: {"_last_result": None}, {"scenario_id": "s"}, object(), None)
    assert col.rows[1].vram_peak_mb == 123.0                # the transformers engine: unchanged


# ---------------------------------------------------------------- the session and server
def test_the_engine_reaches_the_session_config_and_settings(tmp_path, monkeypatch):
    from agentmeter.ingest.run import process_csv
    from agentmeter.session.analyses import settings_fingerprint, settings_of
    from agentmeter.session.benchmark import SessionError, prepare_session
    process_csv(PROJECT_ROOT / "data" / "sample_csv" / "cicids2017_sample.csv", out_root=tmp_path, max_flows=4)
    run = tmp_path / "cicids2017_sample"
    monkeypatch.setenv("AGENTMETER_ENGINE", "vllm")
    plan = prepare_session(run, ["Qwen/Qwen2.5-7B-Instruct"], base_config=str(L4), provider="hf")
    data = yaml.safe_load(open(plan["config_path"]))
    assert data["model"]["engine"] == "vllm"
    st = settings_of(data)
    assert st["model.engine"] == "vllm"
    monkeypatch.setenv("AGENTMETER_ENGINE", "transformers")
    plan = prepare_session(run, ["Qwen/Qwen2.5-7B-Instruct"], base_config=str(L4), provider="hf")
    plain = yaml.safe_load(open(plan["config_path"]))
    assert "engine" not in plain["model"] and "model.engine" not in settings_of(plain)
    assert settings_fingerprint(settings_of(plain)) != settings_fingerprint(st)
    # the mock demo never takes an engine
    monkeypatch.setenv("AGENTMETER_ENGINE", "vllm")
    plan = prepare_session(run, ["mock-a"], provider="mock")
    assert "engine" not in yaml.safe_load(open(plan["config_path"]))["model"]
    monkeypatch.setenv("AGENTMETER_ENGINE", "tgi")
    with pytest.raises(SessionError, match="unknown engine"):
        prepare_session(run, ["Qwen/Qwen2.5-7B-Instruct"], base_config=str(L4), provider="hf")


def test_runtime_reports_and_checks_the_engine(monkeypatch):
    from agentmeter.server import runtime
    monkeypatch.delenv("AGENTMETER_ENGINE", raising=False)
    assert runtime.engine() == "transformers"
    monkeypatch.setenv("AGENTMETER_ENGINE", "vllm")
    assert runtime.engine() == "vllm"
    monkeypatch.setattr(runtime, "gpu_info", lambda: {"cuda_available": True, "torch": "2"})
    monkeypatch.setattr(runtime, "_version", lambda name: None if name == "vllm" else "1.0")
    with pytest.raises(runtime.RealModeError, match="vLLM is not installed"):
        runtime.preflight(require_control_plane=False)
    monkeypatch.setenv("AGENTMETER_ENGINE", "llamacpp")
    with pytest.raises(runtime.RealModeError, match="must be one of transformers, vllm"):
        runtime.preflight(require_control_plane=False)
