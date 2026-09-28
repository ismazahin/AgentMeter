"""Phase 22 — Hugging Face Hub model metadata enrichment (READ-ONLY context).

Pulls a small, clean metadata summary for a model from the public Hugging Face
Hub REST API (https://huggingface.co/api/models/<id>) — parameters, downloads,
likes, license, last-modified, pipeline tag — for display ALONGSIDE the ranking.

This is CONTEXT ONLY. It never touches the locked study DB, the analysis, the SAW
ranking, accuracy, or any measured number, and it is never used to re-rank models
or compute a score. Responses are cached (default 24h) in the SEPARATE app-metadata
DB (via AppStore), never the locked study DB. If the API is unreachable or
rate-limited, callers get an "unavailable"/"stale" status — never an exception.

No new dependency: the default fetcher uses urllib (stdlib). The fetcher is
injectable so tests run fully offline with a mocked HTTP client.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

HF_API_BASE = "https://huggingface.co/api/models/"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h
DEFAULT_TIMEOUT = 8.0

# The five canonical study models (kept in one place; mirrors the locked config).
CANONICAL_MODELS = [
    "mistralai/Mistral-7B-Instruct-v0.3",
    "meta-llama/Meta-Llama-3-8B-Instruct",
    "Qwen/Qwen2.5-7B-Instruct",
    "microsoft/Phi-3-mini-4k-instruct",
    "google/gemma-2-9b-it",
]


def default_fetcher(model_id: str, token: Optional[str] = None,
                    timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """GET the public HF Hub model record as JSON. Raises on any HTTP/network
    error (429, timeout, offline, 404) — get_metadata catches and degrades."""
    url = HF_API_BASE + str(model_id).strip("/")
    req = urllib.request.Request(url, headers={"User-Agent": "AgentMeter/metadata"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read().decode("utf-8"))


def _param_count(raw: dict[str, Any]) -> Optional[int]:
    """Best-effort parameter count from the HF safetensors index (no weights
    downloaded). Returns None when not advertised."""
    st = raw.get("safetensors")
    if isinstance(st, dict):
        total = st.get("total")
        if total is not None:
            try:
                return int(total)
            except (TypeError, ValueError):
                pass
        params = st.get("parameters")
        if isinstance(params, dict) and params:
            try:
                return int(sum(int(v) for v in params.values()))
            except (TypeError, ValueError):
                pass
    return None


def _license(raw: dict[str, Any]) -> Optional[str]:
    """License from cardData, a top-level field, or a `license:<id>` tag."""
    card = raw.get("cardData")
    if isinstance(card, dict) and card.get("license"):
        return str(card["license"])
    if raw.get("license"):
        return str(raw["license"])
    for tag in raw.get("tags", []) or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


def parse_metadata(raw: dict[str, Any], model_id: str) -> dict[str, Any]:
    """Reduce a raw HF API record to the small, clean shape the dashboard shows."""
    params = _param_count(raw)
    return {
        "model": model_id,
        "params": params,
        "params_b": (round(params / 1e9, 2) if params is not None else None),
        "downloads": raw.get("downloads"),
        "likes": raw.get("likes"),
        "license": _license(raw),
        "last_modified": raw.get("lastModified") or raw.get("last_modified"),
        "pipeline_tag": raw.get("pipeline_tag"),
    }


def get_metadata(
    model_id: str,
    *,
    fetcher: Optional[Callable[..., dict[str, Any]]] = None,
    cache: Any = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Return a model's metadata, using a fresh cache entry when available, else
    fetching once and caching the result.

    `cache` is any object with `get(model_id) -> entry | None` and
    `set(model_id, entry)`, where entry = {"fetched_at", "status", "data"}.
    Never raises: on a failed fetch it returns a stale cache (status "stale") if
    one exists, otherwise {"model", "status": "unavailable", "error"}.
    """
    now = time.time() if now is None else now
    fetch = fetcher or default_fetcher

    cached = None
    if cache is not None:
        try:
            cached = cache.get(model_id)
        except Exception:  # noqa: BLE001 — cache must never break the call
            cached = None

    if (cached and cached.get("status") == "ok" and cached.get("data")
            and (now - float(cached.get("fetched_at", 0))) < ttl_seconds):
        out = dict(cached["data"])
        out["status"] = "ok"
        out["cached"] = True
        return out

    try:
        raw = fetch(model_id, token=token)
        data = parse_metadata(raw, model_id)
        if cache is not None:
            try:
                cache.set(model_id, {"fetched_at": now, "status": "ok", "data": data})
            except Exception:  # noqa: BLE001
                pass
        out = dict(data)
        out["status"] = "ok"
        out["cached"] = False
        return out
    except Exception as e:  # noqa: BLE001 — 429 / timeout / offline / 404 / parse
        if cached and cached.get("data"):
            out = dict(cached["data"])
            out["status"] = "stale"
            out["cached"] = True
            out["error"] = str(e)
            return out
        return {"model": model_id, "status": "unavailable", "error": str(e)}


def get_many(
    model_ids: list[str],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Metadata for several models (order preserved). Each is independent, so one
    model being unavailable never affects the others."""
    return [get_metadata(m, **kwargs) for m in model_ids]


def make_store_cache(store: Any) -> Any:
    """Adapt an appdb.AppStore into the get/set cache interface (stores entries in
    the SEPARATE app-metadata DB, never the locked study DB)."""
    class _StoreCache:
        def get(self, model_id: str):
            return store.hf_cache_get(model_id)

        def set(self, model_id: str, entry: dict[str, Any]):
            store.hf_cache_set(model_id, entry)

    return _StoreCache()


def token_from_env() -> Optional[str]:
    """HF token for higher rate limits if present; never required, never exposed."""
    return os.environ.get("HF_TOKEN") or None
