"""Phase 11 — Vast.ai self-destroy (cost safety).

A pull/eval run on a rented Vast.ai GPU instance costs money for every minute the
box is alive. This module lets the server DESTROY its own instance once work is
safely persisted (or after an idle timeout), so I am not billed for idle time.

DESTROY, not stop/pause: a paused instance still bills for storage. We destroy.

Everything degrades safely with NO credentials: if VAST_API_KEY or the instance id
is missing, destroy_instance() logs a clear warning, makes NO HTTP call, and
returns False — it never raises. Nothing is hard-coded; the key and id come from
env/args only.

The instance id is read (in priority order) from an explicit argument, then
VAST_INSTANCE_ID / VAST_CONTAINERLABEL-style env vars Vast.ai sets on the box.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

log = logging.getLogger("agentmeter.vast_shutdown")

# Vast.ai REST API. The instance-management endpoint destroys an instance with a
# DELETE to /api/v0/instances/<id>/ (Bearer VAST_API_KEY).
VAST_API_BASE = os.environ.get("VAST_API_BASE", "https://console.vast.ai/api/v0")

# Env vars that may carry the instance id on a Vast.ai box (checked in order).
_INSTANCE_ID_ENV = ("VAST_INSTANCE_ID", "VAST_CONTAINER_ID", "CONTAINER_ID",
                    "VAST_CONTAINERLABEL")


def get_api_key() -> Optional[str]:
    key = os.environ.get("VAST_API_KEY")
    return key.strip() if key else None


def get_instance_id(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the instance id: explicit arg first, then known env vars.

    Vast's VAST_CONTAINERLABEL is usually like "C.1234567" — we keep only the
    trailing digits so it works as an instance id."""
    if explicit:
        return str(explicit).strip() or None
    for name in _INSTANCE_ID_ENV:
        raw = os.environ.get(name)
        if not raw:
            continue
        raw = raw.strip()
        if not raw:
            continue
        # normalise a "C.1234567" container label to its numeric id
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits or raw
    return None


def destroy_instance(
    instance_id: Optional[str] = None,
    api_key: Optional[str] = None,
    retries: int = 3,
    backoff_base: float = 2.0,
    http_delete: Optional[Callable[..., Any]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """DESTROY this Vast.ai instance. Returns True on success, False otherwise.

    Safe by construction:
      * Missing key/id -> warn, NO HTTP call, return False (never raise).
      * HTTP errors -> retry with exponential backoff, then give up and return
        False. This function NEVER raises out to the caller, so a failed destroy
        can't crash the process after results are already persisted.

    `http_delete` is injectable for tests (defaults to requests.delete) so the
    verify suite makes NO real network call.
    """
    key = api_key or get_api_key()
    iid = get_instance_id(instance_id)
    if not key or not iid:
        log.warning(
            "Vast self-destroy skipped: missing %s%s%s. Set VAST_API_KEY and the "
            "instance id (VAST_INSTANCE_ID or --instance-id) to enable it. The "
            "instance will keep running — destroy it manually to stop billing.",
            "" if key else "VAST_API_KEY",
            "" if (key or iid) else " and ",
            "" if iid else "instance id",
        )
        return False

    delete = http_delete
    if delete is None:
        try:
            import requests

            delete = requests.delete
        except Exception as e:  # noqa: BLE001
            log.warning("Vast self-destroy skipped: requests unavailable (%s).", e)
            return False

    url = f"{VAST_API_BASE.rstrip('/')}/instances/{iid}/"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    for attempt in range(1, retries + 1):
        try:
            resp = delete(url, headers=headers, timeout=15)
            status = getattr(resp, "status_code", None)
            if status is not None and 200 <= status < 300:
                log.warning("Vast instance %s DESTROYED (HTTP %s). Billing stops.",
                            iid, status)
                return True
            log.warning("Vast destroy attempt %d/%d for instance %s returned HTTP %s.",
                        attempt, retries, iid, status)
        except Exception as e:  # noqa: BLE001 — never propagate
            log.warning("Vast destroy attempt %d/%d for instance %s failed: %s",
                        attempt, retries, iid, e)
        if attempt < retries:
            sleep(backoff_base ** attempt)

    log.error("Vast self-destroy FAILED after %d attempts for instance %s. Destroy "
              "it manually in the Vast.ai console to stop billing.", retries, iid)
    return False
