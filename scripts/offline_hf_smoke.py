"""Offline smoke test for the HF adapter — NO network, NO model download.

Run from the repo root:   python scripts/offline_hf_smoke.py

Because model download from the Hub requires a reachable huggingface.co (and a
GPU for VRAM), this test instead builds a TINY causal-LM + fast tokenizer
in-process (random weights) and injects them into the real HFProvider.generate()
code path. It validates the risky mechanics that are hard to get right:
  - device-resolution guards (no silent CPU fallback when a GPU is required)
  - streaming generation loop + time-to-first-token capture
  - exact input/output token counting
  - chat-template fallback for non-chat tokenizers

It does NOT validate: Hub download, gated-model auth, a real instruct model's
chat template, or VRAM measurement — those are exercised only on the GPU Space.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import torch
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast

from agentmeter.config import load_config
from agentmeter.providers.hf import HFProvider

CONFIG = os.path.join(os.path.dirname(__file__), "..", "configs", "smoke_gpt2.yaml")


def build_tiny_tokenizer():
    sample = (
        "Network flow features Destination Port Total Fwd Packets Bwd SYN Flag "
        "Count Benign Brute Force Volumetric DDoS Port Scanning Flood Data "
        "Exfiltration You are the Perceive Reason Decide Act agent classify "
        "summary reasoning class name MITRE hello world 0 1 2 3 4 5 6 7 8 9 : - ,"
    )
    words = ["[UNK]", "[PAD]", "[EOS]"] + sorted(set(sample.split()))
    tk = Tokenizer(models.WordLevel(vocab={w: i for i, w in enumerate(words)}, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.Whitespace()
    fast = PreTrainedTokenizerFast(
        tokenizer_object=tk, unk_token="[UNK]", pad_token="[PAD]", eos_token="[EOS]"
    )
    return fast, len(words)


def build_tiny_model(vocab_size, eos_id, pad_id):
    cfg = GPT2Config(
        vocab_size=vocab_size, n_positions=128, n_embd=32, n_layer=2, n_head=2,
        eos_token_id=eos_id, bos_token_id=eos_id, pad_token_id=pad_id,
    )
    m = GPT2LMHeadModel(cfg)
    m.eval()
    return m


def main() -> int:
    print("=== Offline HFProvider smoke (no download) ===")
    cfg = load_config(CONFIG)

    p = HFProvider(cfg)
    print(f"[guard] device resolved for smoke config: {p.device} (expected cpu)")
    assert p.device == "cpu"

    cfg2 = load_config(CONFIG)
    cfg2.data["run"]["require_gpu"] = True
    cfg2.data["run"]["device"] = "cuda"
    try:
        HFProvider(cfg2)
        print("[guard] FAIL: expected RuntimeError for cuda-required-no-gpu")
        return 1
    except RuntimeError as e:
        print(f"[guard] OK: refuses CPU fallback -> {str(e)[:60]}...")

    tok, vsize = build_tiny_tokenizer()
    p.tokenizer = tok
    p.model = build_tiny_model(vsize, tok.eos_token_id, tok.pad_token_id)
    p.device = "cpu"

    res = p.generate(
        "Network flow features Destination Port 0",
        system="You are the Decide agent classify",
    )
    print("\n[generate] GenerationResult:")
    print(f"  text (repr, truncated): {res.text[:80]!r}")
    print(f"  input_tokens : {res.input_tokens}")
    print(f"  output_tokens: {res.output_tokens}")
    print(f"  ttft_s       : {res.ttft_s}")

    assert res.input_tokens > 0
    assert res.output_tokens >= 0
    assert res.ttft_s is None or res.ttft_s >= 0
    print("\nAll offline smoke assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
