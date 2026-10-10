"""GPU hourly price for the cost-per-1,000-flows analysis (Phase 46).

Order of sources (the first that answers wins; every result records its source and
fetched_at):
  1. Vast.ai API — when VAST_API_KEY and an instance id (VAST_INSTANCE_ID, or the
     CONTAINER_ID Vast sets inside an instance) are present. GET
     https://console.vast.ai/api/v0/instances/<id>/ -> dph_total (USD per hour).
     SERVER-SIDE ONLY: the key is read from the environment, sent only to
     console.vast.ai, and never written to a result, a log line or a response.
  2. config: pricing.gpu_usd_per_hour in the session's base config (or config.yaml).
  3. none — cost is reported as no_data with the reason.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

VAST_API = "https://console.vast.ai/api/v0/instances/{id}/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def instance_id() -> Optional[str]:
    v = os.environ.get("VAST_INSTANCE_ID") or os.environ.get("CONTAINER_ID")
    v = (v or "").strip()
    return v if v.isdigit() else None


def vast_fetch(iid: str, key: str, timeout: float = 5.0) -> dict[str, Any]:
    req = urllib.request.Request(VAST_API.format(id=iid),
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "User-Agent": "AgentMeter/price"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read().decode("utf-8"))


def _dph(data: Any) -> Optional[float]:
    inst = data.get("instances", data) if isinstance(data, dict) else None
    if isinstance(inst, list):
        inst = inst[0] if inst else None
    if not isinstance(inst, dict):
        return None
    for k in ("dph_total", "dph"):
        try:
            if inst.get(k) is not None:
                return float(inst[k])
        except (TypeError, ValueError):
            continue
    return None


def resolve_price(config_price: Optional[float] = None, *,
                  fetcher: Callable[[str, str], dict] = vast_fetch) -> dict[str, Any]:
    """{"usd_per_hour", "source", "fetched_at", ...}; never raises, never returns the key."""
    key = os.environ.get("VAST_API_KEY")
    iid = instance_id()
    err = None
    if key and iid:
        try:
            dph = _dph(fetcher(iid, key))
            if dph is not None:
                return {"usd_per_hour": dph, "source": "vast_api", "instance_id": iid, "fetched_at": _now()}
            err = "Vast API answer had no dph_total"
        except Exception as e:  # noqa: BLE001
            err = f"Vast API unavailable: {type(e).__name__}"
            if key in str(e):                       # never leak the key through an error text
                err = "Vast API unavailable"
            else:
                err += f": {e}"
    if config_price is not None:
        try:
            out = {"usd_per_hour": float(config_price), "source": "config (pricing.gpu_usd_per_hour)",
                   "fetched_at": _now()}
            if err:
                out["error"] = err
            return out
        except (TypeError, ValueError):
            err = f"pricing.gpu_usd_per_hour is not a number ({config_price!r})"
    return {"usd_per_hour": None, "source": None, "fetched_at": None, "error": err,
            "reason": ("no GPU price: set pricing.gpu_usd_per_hour in config.yaml, or VAST_API_KEY + "
                       "VAST_INSTANCE_ID on the Vast backend")}
