"""vLLM engine for open models, 4-bit NF4 only (an alternative to the in-process
transformers engine of hf.py).

Same models (open-weight Hugging Face repos, original weights), same quantization:
vLLM's in-flight bitsandbytes path quantizes every linear layer to 4-bit NF4 with double
quantization at load time (vllm/model_executor/model_loader/bitsandbytes_loader.py:
quantize_4bit(..., compress_statistics=True, quant_type="nf4")), the format hf.py uses.
Any other quantization (none, 8-bit, GPTQ, AWQ, FP8, ...) is refused here, and a repo
that is already quantized is blocked by the model catalog before it gets here.

What differs from the transformers engine, and is reported as such:
  - continuous batching and paged attention: under concurrency (the opt-in load test)
    requests in flight are batched together, so throughput can scale;
  - vLLM reserves a fixed share of GPU memory up front (gpu_memory_utilization) for its
    KV cache, so a per-agent working-VRAM delta is meaningless: per-call VRAM is not
    recorded (None); the total device peak (NVML) still is;
  - the offline engine does not stream, so time-to-first-token is not measured (None).
The engine is part of the session's settings (model.engine): a vLLM session is never
like-for-like with a transformers one.

The engine runs in this process (VLLM_ENABLE_V1_MULTIPROCESSING=0), one model per worker
subprocess, as with hf.py: the OS reclaims the GPU when the worker exits.
"""
from __future__ import annotations

import os
import queue
import threading
from typing import Any, Optional

from ..config import Config
from .base import GenerationResult, ModelProvider

ENGINE = "vllm"
QUANTIZATION = "bitsandbytes"     # vLLM's in-flight 4-bit NF4 (double quantization)


class VLLMConfigError(ValueError):
    """The vLLM engine was asked for something other than 4-bit NF4."""


def check_quantization(config: Config) -> None:
    """The study's quantization, and nothing else: 4-bit NF4 with double quantization."""
    q = (config.get("model.hf", {}) or {}).get("quantization", {}) or {}
    problems = []
    if not q.get("load_in_4bit"):
        problems.append("load_in_4bit must be true")
    if q.get("load_in_8bit"):
        problems.append("8-bit is not allowed")
    if q.get("bnb_4bit_quant_type", "nf4") != "nf4":
        problems.append(f"bnb_4bit_quant_type must be nf4 (got {q.get('bnb_4bit_quant_type')!r})")
    if not q.get("bnb_4bit_use_double_quant", True):
        problems.append("bnb_4bit_use_double_quant must be true (vLLM always double-quantizes)")
    v = config.get("model.vllm", {}) or {}
    if v.get("quantization", QUANTIZATION) != QUANTIZATION:
        problems.append(f"model.vllm.quantization must be {QUANTIZATION!r} (4-bit NF4); got {v.get('quantization')!r}")
    if problems:
        raise VLLMConfigError("the vLLM engine runs open models in 4-bit NF4 only: " + "; ".join(problems))


class VLLMProvider(ModelProvider):
    name = ENGINE
    measures_call_vram = False       # vLLM pre-allocates its KV cache: no per-call working VRAM
    measures_ttft = False

    def __init__(self, config: Config):
        check_quantization(config)
        self.config = config
        self.model_name: str = config.get("model.name")
        hf = config.get("model.hf", {}) or {}
        v = config.get("model.vllm", {}) or {}
        self.token_env: str = hf.get("token_env", "HF_TOKEN")
        self.dtype_name: str = hf.get("dtype", "float16")
        self.max_new_tokens: int = int(hf.get("max_new_tokens", 256))
        self.temperature: float = float(hf.get("temperature", 0.0)) if hf.get("do_sample") else 0.0
        self.gpu_memory_utilization: float = float(v.get("gpu_memory_utilization", 0.85))
        self.max_model_len: Optional[int] = int(v["max_model_len"]) if v.get("max_model_len") else 8192
        self.enforce_eager: bool = bool(v.get("enforce_eager", True))
        self.seed: int = int(v.get("seed", 0))
        if not 0.1 <= self.gpu_memory_utilization <= 0.95:
            raise VLLMConfigError("model.vllm.gpu_memory_utilization must be between 0.1 and 0.95")
        self.llm = None
        self.tokenizer = None
        self.quantization_label = "4bit-nf4"
        self._q: "queue.Queue[tuple[str, int, dict]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # --- lifecycle ------------------------------------------------------
    def engine_kwargs(self) -> dict[str, Any]:
        return {"model": self.model_name, "quantization": QUANTIZATION, "load_format": QUANTIZATION,
                "dtype": self.dtype_name, "gpu_memory_utilization": self.gpu_memory_utilization,
                "max_model_len": self.max_model_len, "enforce_eager": self.enforce_eager,
                "seed": self.seed, "trust_remote_code": False, "tensor_parallel_size": 1}

    def load(self) -> None:
        os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")   # same process: NVML/torch see it
        token = os.environ.get(self.token_env)
        if token:
            os.environ.setdefault("HF_TOKEN", token)
        from vllm import LLM
        self.llm = LLM(**self.engine_kwargs())
        self.tokenizer = self.llm.get_tokenizer()
        self._stop.clear()
        self._worker = threading.Thread(target=self._serve, name="agentmeter-vllm", daemon=True)
        self._worker.start()

    def unload(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._q.put(("", 0, {}))             # wake the dispatcher so it can exit
            self._worker.join(timeout=10)
        self._worker = None
        self.llm = None
        self.tokenizer = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
        except Exception:
            pass

    def count_tokens(self, text: str) -> Optional[int]:
        if not text:
            return 0
        if self.tokenizer is None:
            return None
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    # --- inference ------------------------------------------------------
    def _prompt(self, prompt: str, system: Optional[str]) -> str:
        """The chat template, as hf.py applies it; a template that rejects a system turn
        gets it folded into the user turn (same fallback as hf.py)."""
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        tok = self.tokenizer
        if getattr(tok, "chat_template", None):
            try:
                return tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            except Exception:
                try:
                    merged = [{"role": "user", "content": (f"{system}\n\n" if system else "") + prompt}]
                    return tok.apply_chat_template(merged, add_generation_prompt=True, tokenize=False)
                except Exception:
                    pass
        return (f"{system}\n\n" if system else "") + prompt

    def _serve(self) -> None:
        """One dispatcher owns the engine (vLLM's LLM is not thread-safe). It takes the next
        request and, without waiting, whatever else is already queued, and runs them as one
        batch: a lone measured call is never delayed, concurrent calls share a batch."""
        from vllm import SamplingParams
        while not self._stop.is_set():
            first = self._q.get()
            if self._stop.is_set():
                break
            batch = [first]
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            batch = [b for b in batch if b[2]]
            if not batch:
                continue
            params = [SamplingParams(max_tokens=n, temperature=self.temperature, seed=self.seed) for _, n, _ in batch]
            try:
                outs = self.llm.generate([p for p, _, _ in batch], params, use_tqdm=False)
                for (_, _, slot), out in zip(batch, outs):
                    slot["out"] = out
            except Exception as e:  # noqa: BLE001 — handed to every waiting caller
                for _, _, slot in batch:
                    slot["error"] = e
            for _, _, slot in batch:
                slot["done"].set()

    def generate(self, prompt: str, system: Optional[str] = None,
                 max_new_tokens: Optional[int] = None) -> GenerationResult:
        if self.llm is None:
            raise RuntimeError("the vLLM engine is not loaded (call load() first)")
        slot: dict = {"done": threading.Event()}
        self._q.put((self._prompt(prompt, system), int(max_new_tokens or self.max_new_tokens), slot))
        slot["done"].wait()
        if "error" in slot:
            raise slot["error"]
        out = slot["out"]
        comp = out.outputs[0]
        return GenerationResult(text=(comp.text or "").strip(),
                                input_tokens=len(out.prompt_token_ids or []),
                                output_tokens=len(comp.token_ids or []),
                                ttft_s=None)
