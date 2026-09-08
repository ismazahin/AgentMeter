"""Phase 4 — Hugging Face in-process model adapter (Mode A).

Pulls a model from the HF Hub and runs it IN-PROCESS on the chosen device, so
torch.cuda / pynvml can read the VRAM this model uses (per-agent VRAM is a core
requirement of the study). No Inference Endpoints, no remote API.

Key behaviours:
  - The model is loaded ONCE per run (Cara 1: same model for all agents).
  - generate() streams tokens so time-to-first-token (TTFT) is measured, and
    returns exact input/output token counts from the tokenizer.
  - Device is explicit: 'cuda' requires a real GPU; there is NO silent CPU
    fallback when run.require_gpu is true (matches the env-check guard).
    'cpu' is allowed only as an explicit dev/smoke choice.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from ..config import Config
from .base import GenerationResult, ModelProvider


class HFProvider(ModelProvider):
    def __init__(self, config: Config):
        import torch  # imported here so Phases 0-3 need no torch

        self._torch = torch
        self.config = config
        self.name: str = config.get("model.name")
        hf = config.get("model.hf", {}) or {}
        self.token_env: str = hf.get("token_env", "HF_TOKEN")
        self.dtype_name: str = hf.get("dtype", "float16")
        self.max_new_tokens: int = int(hf.get("max_new_tokens", 256))
        self.temperature: float = float(hf.get("temperature", 0.0))
        self.do_sample: bool = bool(hf.get("do_sample", False))

        self.require_gpu: bool = bool(config.get("run.require_gpu", False))
        self.device = self._resolve_device(config.get("run.device", "auto"))

        self.tokenizer = None
        self.model = None
        self.model_vram_mb: Optional[float] = None  # weight footprint, diagnostic

    # --- device / dtype -------------------------------------------------
    def _resolve_device(self, requested: str) -> str:
        cuda = self._torch.cuda.is_available()
        requested = (requested or "auto").lower()
        if requested == "cuda":
            if not cuda:
                raise RuntimeError(
                    "run.device='cuda' but torch.cuda is not available. "
                    "Run on the GPU host — refusing to fall back to CPU."
                )
            return "cuda"
        if requested == "cpu":
            if self.require_gpu:
                raise RuntimeError(
                    "run.require_gpu is true but run.device='cpu'. VRAM cannot be "
                    "measured on CPU — refusing this contradictory configuration."
                )
            return "cpu"
        # auto
        if cuda:
            return "cuda"
        if self.require_gpu:
            raise RuntimeError(
                "run.require_gpu is true but no CUDA device is available "
                "(run.device=auto). Refusing to fall back to CPU."
            )
        return "cpu"

    def _dtype(self):
        torch = self._torch
        if self.device == "cpu":
            return torch.float32  # float16 is slow/unsupported for many CPU ops
        return {"float16": torch.float16, "bfloat16": torch.bfloat16, "float32": torch.float32}.get(
            self.dtype_name, torch.float16
        )

    # --- lifecycle ------------------------------------------------------
    def load(self) -> None:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        token = os.environ.get(self.token_env)
        if token:
            try:
                from huggingface_hub import login

                login(token=token)
            except Exception:
                pass  # non-gated models still load without login

        self.tokenizer = AutoTokenizer.from_pretrained(self.name, token=token)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # transformers >=5 renamed torch_dtype -> dtype; support both.
        try:
            self.model = AutoModelForCausalLM.from_pretrained(
                self.name, dtype=self._dtype(), token=token
            )
        except TypeError:
            self.model = AutoModelForCausalLM.from_pretrained(
                self.name, torch_dtype=self._dtype(), token=token
            )
        self.model.to(self.device)
        self.model.eval()

        if self.device == "cuda":
            self._torch.cuda.synchronize()
            self.model_vram_mb = self._torch.cuda.memory_allocated() / (1024 * 1024)

    def unload(self) -> None:
        self.model = None
        self.tokenizer = None
        if self.device == "cuda":
            import gc

            gc.collect()
            self._torch.cuda.empty_cache()

    # --- inference ------------------------------------------------------
    def _encode(self, prompt: str, system: Optional[str]):
        """Build input_ids using the chat template when available."""
        tok = self.tokenizer
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        if getattr(tok, "chat_template", None):
            try:
                return tok.apply_chat_template(
                    messages, add_generation_prompt=True, return_tensors="pt"
                ).to(self.device)
            except Exception:
                # Some templates reject a 'system' role — fold it into the user turn.
                merged = (f"{system}\n\n" if system else "") + prompt
                return tok(merged, return_tensors="pt").input_ids.to(self.device)

        merged = (f"{system}\n\n" if system else "") + prompt
        return tok(merged, return_tensors="pt").input_ids.to(self.device)

    def generate(self, prompt: str, system: Optional[str] = None) -> GenerationResult:
        from transformers import TextIteratorStreamer

        torch = self._torch
        input_ids = self._encode(prompt, system)
        input_len = int(input_ids.shape[-1])

        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True
        )
        gen_kwargs = dict(
            input_ids=input_ids,
            max_new_tokens=self.max_new_tokens,
            do_sample=self.do_sample,
            pad_token_id=self.tokenizer.pad_token_id,
            streamer=streamer,
        )
        if self.do_sample:
            gen_kwargs["temperature"] = self.temperature

        container: dict = {}

        def _run():
            with torch.inference_mode():
                container["out"] = self.model.generate(**gen_kwargs)

        t0 = time.perf_counter()
        thread = threading.Thread(target=_run)
        thread.start()

        ttft: Optional[float] = None
        pieces: list[str] = []
        for chunk in streamer:
            if ttft is None and chunk:
                ttft = time.perf_counter() - t0
            pieces.append(chunk)
        thread.join()

        text = "".join(pieces).strip()

        # Exact output token count from the returned sequence.
        out = container.get("out")
        if out is not None:
            output_tokens = int(out.shape[-1]) - input_len
        else:
            output_tokens = len(self.tokenizer(text, add_special_tokens=False).input_ids)

        return GenerationResult(
            text=text,
            input_tokens=input_len,
            output_tokens=max(0, output_tokens),
            ttft_s=ttft,
        )
