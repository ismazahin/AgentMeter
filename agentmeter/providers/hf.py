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

        # Quantization (config-driven; declared as a methodology change in the
        # thesis). Off by default. 4-bit NF4 is what fits a 7-8B model on a T4.
        quant = hf.get("quantization", {}) or {}
        self.load_in_4bit: bool = bool(quant.get("load_in_4bit", False))
        self.load_in_8bit: bool = bool(quant.get("load_in_8bit", False))
        self.bnb_4bit_quant_type: str = quant.get("bnb_4bit_quant_type", "nf4")
        self.bnb_4bit_use_double_quant: bool = bool(quant.get("bnb_4bit_use_double_quant", True))
        self.bnb_4bit_compute_dtype: str = quant.get("bnb_4bit_compute_dtype", "float16")

        self.require_gpu: bool = bool(config.get("run.require_gpu", False))
        self.device = self._resolve_device(config.get("run.device", "auto"))

        self.quantized: bool = self.load_in_4bit or self.load_in_8bit
        if self.quantized and self.device != "cuda":
            raise RuntimeError("Quantization (bitsandbytes) requires a CUDA device.")

        self.tokenizer = None
        self.model = None
        self.model_vram_mb: Optional[float] = None  # weight footprint, diagnostic
        self.quantization_label: str = (
            "4bit-" + self.bnb_4bit_quant_type if self.load_in_4bit
            else "8bit" if self.load_in_8bit
            else "none"
        )

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

        kwargs: dict = {"token": token}
        if self.quantized:
            # bitsandbytes: model must be placed via device_map at load time and
            # must NOT be moved with .to() afterward.
            from transformers import BitsAndBytesConfig

            if self.load_in_4bit:
                bnb = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type=self.bnb_4bit_quant_type,
                    bnb_4bit_use_double_quant=self.bnb_4bit_use_double_quant,
                    bnb_4bit_compute_dtype=getattr(self._torch, self.bnb_4bit_compute_dtype),
                )
            else:
                bnb = BitsAndBytesConfig(load_in_8bit=True)
            kwargs["quantization_config"] = bnb
            kwargs["device_map"] = {"": 0}
            self.model = self._from_pretrained(**kwargs)
        else:
            kwargs["_dtype"] = self._dtype()
            self.model = self._from_pretrained(**kwargs)
            self.model.to(self.device)
        self.model.eval()

        if self.device == "cuda":
            self._torch.cuda.synchronize()
            self.model_vram_mb = self._torch.cuda.memory_allocated() / (1024 * 1024)

    def _from_pretrained(self, _dtype=None, **kwargs):
        """from_pretrained with the transformers 4.x/5.x dtype-kwarg shim."""
        from transformers import AutoModelForCausalLM

        if _dtype is None:
            return AutoModelForCausalLM.from_pretrained(self.name, **kwargs)
        try:
            return AutoModelForCausalLM.from_pretrained(self.name, dtype=_dtype, **kwargs)
        except TypeError:
            return AutoModelForCausalLM.from_pretrained(self.name, torch_dtype=_dtype, **kwargs)

    def unload(self) -> None:
        self.model = None
        self.tokenizer = None
        if self.device == "cuda":
            import gc

            gc.collect()
            self._torch.cuda.empty_cache()

    # --- inference ------------------------------------------------------
    def _encode(self, prompt: str, system: Optional[str]) -> dict:
        """Encode one turn into a dict of tensors on the device.

        Always returns a mapping with at least 'input_ids' (and usually
        'attention_mask'), so callers read input_ids consistently. This is
        deliberately robust to apply_chat_template returning either a bare
        tensor (older transformers) or a BatchEncoding (transformers >=5,
        where return_dict defaults to True).
        """
        tok = self.tokenizer
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        enc = None
        if getattr(tok, "chat_template", None):
            enc = self._apply_chat_template(messages)
        if enc is None:
            # No template, or it rejected the messages (e.g. a 'system' role) —
            # fold system into the user turn and tokenize plainly.
            merged = (f"{system}\n\n" if system else "") + prompt
            enc = tok(merged, return_tensors="pt")

        # Normalize a bare tensor into a dict.
        if hasattr(enc, "shape") and not hasattr(enc, "items"):
            enc = {"input_ids": enc}
        return {k: v.to(self.device) for k, v in enc.items()}

    def _apply_chat_template(self, messages):
        """Return a BatchEncoding/dict for the chat template, or None on failure."""
        tok = self.tokenizer
        try:
            return tok.apply_chat_template(
                messages, add_generation_prompt=True, return_tensors="pt", return_dict=True
            )
        except TypeError:
            # Older transformers without return_dict: returns a bare tensor.
            try:
                ids = tok.apply_chat_template(
                    messages, add_generation_prompt=True, return_tensors="pt"
                )
                return {"input_ids": ids}
            except Exception:
                return None
        except Exception:
            return None

    def generate(self, prompt: str, system: Optional[str] = None) -> GenerationResult:
        from transformers import TextIteratorStreamer

        torch = self._torch
        inputs = self._encode(prompt, system)
        input_len = int(inputs["input_ids"].shape[-1])

        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True
        )
        gen_kwargs = dict(
            **inputs,
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
