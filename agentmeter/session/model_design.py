"""A model's published design, read from its own files in the local Hugging Face download.

Used only as LIKELY reasons in the report's explanations (design:
docs/superpowers/specs/2026-11-24-why-each-model-design.md). Nothing here is measured, and nothing
is fetched from the internet: the GPU machine already downloaded every benchmarked model, gated ones
included, so their config.json is on disk when the session ends. A model whose files are not there
simply has no design facts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

# bytes per parameter of the stored weights (the index's total_size is in bytes)
_DTYPE_BYTES = {"float32": 4, "float16": 2, "bfloat16": 2, "float8_e4m3fn": 1, "int8": 1}


def _cached_file(model_id: str, name: str) -> Optional[Path]:
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:  # pragma: no cover - hub not installed (CPU / demo host)
        return None
    try:
        p = try_to_load_from_cache(model_id, name)
    except Exception:
        return None
    return Path(p) if isinstance(p, str) and Path(p).is_file() else None


def design_from_config(cfg: dict[str, Any], index: Optional[dict] = None) -> dict[str, Any]:
    """The design facts of one model from its config.json (and safetensors index, for size)."""
    c = dict(cfg.get("text_config") or {}, **{k: v for k, v in cfg.items() if k != "text_config"})
    layers = c.get("num_hidden_layers")
    heads = c.get("num_attention_heads")
    kv = c.get("num_key_value_heads") or heads
    hidden = c.get("hidden_size")
    head_dim = c.get("head_dim") or (hidden // heads if hidden and heads else None)
    out: dict[str, Any] = {
        "model_type": c.get("model_type"),
        "layers": layers, "attention_heads": heads, "kv_heads": kv, "head_dim": head_dim,
        "hidden_size": hidden, "vocab_size": c.get("vocab_size"),
        "context_window": c.get("max_position_embeddings"),
    }
    if layers and kv and head_dim:
        # keys and values cached for every token, per layer (elements, not bytes)
        out["kv_cache_per_token"] = int(layers) * int(kv) * int(head_dim) * 2
        out["kv_heads_x_layers"] = int(layers) * int(kv)
    size = ((index or {}).get("metadata") or {}).get("total_size")
    per = _DTYPE_BYTES.get(str(c.get("torch_dtype") or c.get("dtype") or "bfloat16"))
    if size and per:
        out["params_b"] = round(float(size) / per / 1e9, 2)
    return {k: v for k, v in out.items() if v is not None}


def read_design(model_id: str, finder: Callable[[str, str], Optional[Path]] = _cached_file) -> Optional[dict]:
    """The design facts of a downloaded model, or None when its config.json is not on this machine."""
    cfg_p = finder(model_id, "config.json")
    if not cfg_p:
        return None
    try:
        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    idx_p = finder(model_id, "model.safetensors.index.json")
    try:
        index = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p else None
    except (OSError, ValueError):
        index = None
    d = design_from_config(cfg, index)
    d["source"] = "config.json of the local model download"
    return d


def read_all(models: list[str], finder: Callable[[str, str], Optional[Path]] = _cached_file) -> dict[str, dict]:
    """{model: design} for every model whose files are on this machine."""
    out = {}
    for m in models:
        d = read_design(str(m).replace("mock:", ""), finder)
        if d:
            out[m] = d
    return out
