"""Extra models users may benchmark next to the 5 study models (model catalog).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

  configs/model_catalog.yaml   the curated list (original-weights Hugging Face repos)
  configs/model_rules.yaml     suitability rule-base: can this model be benchmarked like the
                               study models — original weights, quantized by AgentMeter to
                               4-bit NF4 at load, on THIS GPU?

For every listed model this module pulls the repo record from the Hugging Face Hub API
(GET /api/models/<id>?blobs=true: parameter count, files and sizes, config, tags, gated,
licence; cached 24 h in memory — never written to a results DB), derives facts from it (pre-quantized?, safetensors?,
remote code?, estimated 4-bit VRAM, download size and time, fits this GPU / disk?) and runs
the rules → status ok | warning | blocked | unknown, with the notes of the rules that fired
and an audit of every rule. Nothing here changes a score; the study models and the locked
baseline are untouched (they are listed separately, read from configs/run_full_l4.yaml).

GET /api/models/catalog serves the result (read run token in control-plane mode).
Fetching never fails the request: an unreachable Hub gives status "unknown" with the reason.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..config import PROJECT_ROOT

CATALOG_PATH = PROJECT_ROOT / "configs" / "model_catalog.yaml"
RULES_PATH = PROJECT_ROOT / "configs" / "model_rules.yaml"
HF_REPO_API = "https://huggingface.co/api/models/{id}?blobs=true"
CACHE_PREFIX = "repo:"          # app-DB cache key, kept apart from hf_metadata's entries
TTL_S = 24 * 3600
TIMEOUT_S = 8.0
# the files a benchmark downloads (same patterns as scripts/vast_models.py)
DOWNLOAD_PATTERNS = ["*.json", "*.safetensors", "tokenizer*", "*.model", "*.tiktoken", "*.txt"]
DOWNLOAD_IGNORE = ["original/*", "*.pth", "*.bin", "*.gguf", "*.onnx", "*.msgpack", "*.h5"]
_QUANT_TAGS = ("gptq", "awq", "gguf", "exl2", "4-bit", "8-bit", "bitsandbytes", "hqq", "aqlm", "fp8")
_QUANT_NAMES = ("-gptq", "-awq", "-gguf", "-exl2", "-4bit", "-8bit", "-int4", "-int8", "-fp8", "-bnb")
QUANTIZATION = ("4-bit NF4 (bitsandbytes, double quantization, fp16 compute), applied by AgentMeter "
                "at load time to the original weights — the same as the study models")
FRAMING = ("Suitability only: whether a model can be benchmarked like the study models (original "
           "weights → 4-bit NF4 on this GPU). It says nothing about threat-detection quality.")


class CatalogError(ValueError):
    """The catalog or rule file is malformed."""


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def load_catalog(path: Optional[str | Path] = None) -> list[dict[str, Any]]:
    p = Path(path or CATALOG_PATH)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise CatalogError(f"cannot read {p}: {e}") from e
    models = data.get("models") or []
    if not isinstance(models, list):
        raise CatalogError(f"{p}: 'models' must be a list")
    seen, out = set(), []
    for m in models:
        mid = str((m or {}).get("id") or "").strip()
        if not mid or "/" not in mid or mid in seen:
            raise CatalogError(f"{p}: every model needs a unique 'owner/name' id (got {mid!r})")
        if any(mid.lower().endswith(s) for s in _QUANT_NAMES):
            raise CatalogError(f"{p}: {mid} looks pre-quantized — list the original-weights repo")
        seen.add(mid)
        out.append({"id": mid, "family": str(m.get("family") or ""), "note": str(m.get("note") or "")})
    return out


def load_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    from ..session.stage3 import OPS

    p = Path(path or RULES_PATH)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise CatalogError(f"cannot read {p}: {e}") from e
    consts, rules = data.get("constants") or {}, data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise CatalogError(f"{p}: 'rules' must be a non-empty list")
    seen = set()
    for r in rules:
        rid = r.get("id")
        if not rid or rid in seen:
            raise CatalogError(f"{p}: every rule needs a unique id (got {rid!r})")
        seen.add(rid)
        if r.get("outcome") not in ("block", "warn"):
            raise CatalogError(f"{p}: rule {rid}: outcome must be block or warn")
        if not r.get("note") or not isinstance(r.get("when"), list) or not r["when"]:
            raise CatalogError(f"{p}: rule {rid}: needs 'when' (list) and 'note'")
        for c in r["when"]:
            if c.get("op") not in OPS:
                raise CatalogError(f"{p}: rule {rid}: unknown op {c.get('op')!r}")
            if not str(c.get("field", "")).startswith(("hf.", "derived.", "env.")):
                raise CatalogError(f"{p}: rule {rid}: field must start with hf./derived./env.")
            v = c.get("value")
            if isinstance(v, str) and v.startswith("$") and v[1:] not in consts:
                raise CatalogError(f"{p}: rule {rid}: unknown constant {v}")
    return {"version": data.get("version", 1), "constants": consts, "rules": rules,
            "source": str(p.relative_to(PROJECT_ROOT) if p.is_relative_to(PROJECT_ROOT) else p)}


# ---------------------------------------------------------------------------
# Hugging Face repo record
# ---------------------------------------------------------------------------
def default_fetcher(model_id: str, token: Optional[str] = None, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    req = urllib.request.Request(HF_REPO_API.format(id=model_id.strip("/")),
                                 headers={"User-Agent": "AgentMeter/model-catalog"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read().decode("utf-8"))


def parse_repo(raw: dict[str, Any], model_id: str) -> dict[str, Any]:
    """The small, clean record the rules read (raw API responses are never stored)."""
    from .hf_metadata import parse_metadata

    md = parse_metadata(raw, model_id)
    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    files = []
    for s in raw.get("siblings") or []:
        name = (s or {}).get("rfilename")
        if name:
            size = s.get("size")
            if size is None and isinstance(s.get("lfs"), dict):
                size = s["lfs"].get("size")
            files.append({"name": str(name), "size": int(size) if isinstance(size, (int, float)) else None})
    return {**md, "tags": [str(t).lower() for t in raw.get("tags") or []], "files": files,
            "model_type": cfg.get("model_type"), "architectures": cfg.get("architectures"),
            "auto_map": bool(cfg.get("auto_map")),
            "quant_method": ((cfg.get("quantization_config") or {}).get("quant_method")
                             if isinstance(cfg.get("quantization_config"), dict) else
                             ("unknown" if cfg.get("quantization_config") else None))}


def fetch_repo(model_id: str, *, fetcher: Optional[Callable] = None, cache: Any = None,
               token: Optional[str] = None, now: Optional[float] = None) -> dict[str, Any]:
    """{"status": ok|stale|unavailable, "repo": {...}|None, "fetched_at", "error"?}. Never raises."""
    from .hf_metadata import _iso

    now = time.time() if now is None else now
    key = CACHE_PREFIX + model_id
    cached = None
    if cache is not None:
        try:
            cached = cache.get(key)
        except Exception:  # noqa: BLE001 — the cache must never break the call
            cached = None
    if cached and cached.get("status") == "ok" and cached.get("data") and now - float(cached.get("fetched_at", 0)) < TTL_S:
        return {"status": "ok", "repo": cached["data"], "fetched_at": _iso(cached["fetched_at"]), "cached": True}
    try:
        repo = parse_repo((fetcher or default_fetcher)(model_id, token=token), model_id)
        if cache is not None:
            try:
                cache.set(key, {"fetched_at": now, "status": "ok", "data": repo})
            except Exception:  # noqa: BLE001
                pass
        return {"status": "ok", "repo": repo, "fetched_at": _iso(now), "cached": False}
    except Exception as e:  # noqa: BLE001 — 404 / 429 / timeout / offline / parse
        if cached and cached.get("data"):
            return {"status": "stale", "repo": cached["data"], "fetched_at": _iso(cached.get("fetched_at")),
                    "cached": True, "error": str(e)}
        return {"status": "unavailable", "repo": None, "fetched_at": None, "error": str(e)}


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------
def _wanted(name: str) -> bool:
    return (any(fnmatch.fnmatch(name, p) for p in DOWNLOAD_PATTERNS)
            and not any(fnmatch.fnmatch(name, p) for p in DOWNLOAD_IGNORE))


def native_support(model_type: Optional[str]) -> Optional[bool]:
    """True/False when transformers is installed (the GPU server); None when it is not."""
    if not model_type:
        return None
    try:
        from transformers.models.auto.configuration_auto import CONFIG_MAPPING_NAMES
    except Exception:  # noqa: BLE001 — not installed here (demo server / CI)
        return None
    return model_type in CONFIG_MAPPING_NAMES


def server_env(gpu_vram_mb: Optional[float] = None, disk_path: Optional[Path] = None) -> dict[str, Any]:
    from . import runtime

    path = disk_path or runtime._hub_cache()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        free_gb = shutil.disk_usage(path).free / 1024 ** 3
    except OSError:
        free_gb = None
    return {"gpu_vram_gb": (gpu_vram_mb / 1024 if gpu_vram_mb else None), "disk_free_gb": free_gb,
            "transformers": runtime._version("transformers")}


def derive(repo: Optional[dict], env: dict, consts: dict, *, on_disk: bool = False,
           native: Optional[Callable[[Optional[str]], Optional[bool]]] = None) -> dict[str, Any]:
    """Facts the rules read: hf.* (from the repo record), derived.*, env.*."""
    f: dict[str, Any] = {f"env.{k}": v for k, v in env.items()}
    f["derived.max_params_b"] = consts.get("max_params_b")
    if not repo:
        return f
    for k in ("params_b", "gated", "license", "pipeline_tag", "downloads", "likes", "last_modified", "source_url"):
        f[f"hf.{k}"] = repo.get(k)
    names = [x["name"] for x in repo.get("files") or []]
    lname, tags = repo["model"].lower(), repo.get("tags") or []
    fmt = repo.get("quant_method") or next((t for t in tags if t in _QUANT_TAGS), None) \
        or next((s.strip("-") for s in _QUANT_NAMES if s in lname), None) \
        or ("gguf" if any(n.lower().endswith(".gguf") for n in names) else None)
    f["derived.prequantized"] = bool(fmt)
    f["derived.quant_format"] = fmt
    f["derived.has_safetensors"] = any(n.endswith(".safetensors") for n in names) if names else None
    f["derived.model_type"] = repo.get("model_type")
    nat = (native or native_support)(repo.get("model_type"))
    f["derived.native_support"] = nat
    custom = repo.get("auto_map") or "custom_code" in tags
    # custom code that transformers now ships natively (e.g. Phi-3) loads without trust_remote_code
    f["derived.needs_remote_code"] = (False if not custom else (False if nat else (True if nat is False else None)))
    pb = repo.get("params_b")
    if pb is not None:
        f["derived.vram_4bit_gb"] = round(pb * consts["gb_per_b_params_4bit"] + consts["overhead_gb"]
                                          + consts["working_gb"], 1)
        if env.get("gpu_vram_gb"):
            f["derived.vram_ratio"] = round(f["derived.vram_4bit_gb"] / env["gpu_vram_gb"], 3)
    sizes = [x["size"] for x in repo.get("files") or [] if _wanted(x["name"])]
    if sizes and all(s is not None for s in sizes):
        gb = 0.0 if on_disk else sum(sizes) / 1024 ** 3
        f["derived.download_gb"] = round(gb, 1)
        f["derived.download_min"] = round(gb * 1024 / consts["assumed_download_mb_s"] / 60, 1)
        if env.get("disk_free_gb") is not None:
            f["derived.disk_ratio"] = round(gb / env["disk_free_gb"], 3) if env["disk_free_gb"] > 0 else None
    return f


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate(model_id: str, facts: dict, rules: dict) -> dict[str, Any]:
    from ..session.stage3 import _check, render

    facts = {**facts, "model": model_id}
    audit, reasons = [], []
    for r in rules["rules"]:
        results = [_check(c, facts, rules["constants"]) for c in r["when"]]
        if any(ok is None for ok, _ in results):
            result, detail = "no_data", "; ".join(t for ok, t in results if ok is None)
        elif all(ok for ok, _ in results):
            result, detail = "fired", "all conditions hold"
        else:
            result, detail = "not_fired", "; ".join(f"not ({t})" for ok, t in results if ok is False)
        row = {"rule": r["id"], "outcome": r["outcome"], "description": r.get("description", ""),
               "result": result, "detail": detail}
        if result == "fired":
            row["note"] = render(r["note"], facts)
            reasons.append({"rule": r["id"], "outcome": r["outcome"], "note": row["note"]})
        audit.append(row)
    return {"reasons": reasons, "audit": audit}


def assess(entry: dict, fetched: dict, env: dict, rules: dict, *, on_disk: bool = False,
           native: Optional[Callable] = None) -> dict[str, Any]:
    repo = fetched.get("repo")
    facts = derive(repo, env, rules["constants"], on_disk=on_disk, native=native)
    ev = evaluate(entry["id"], facts, rules)
    if any(x["outcome"] == "block" for x in ev["reasons"]):
        status = "blocked"
    elif not repo:
        status = "unknown"
    elif ev["reasons"]:
        status = "warning"
    else:
        status = "ok"
    g = lambda k: facts.get(k)  # noqa: E731
    return {
        "id": entry["id"], "family": entry.get("family"), "note": entry.get("note"),
        "status": status, "available": status in ("ok", "warning"),
        "reasons": ev["reasons"] + ([{"rule": "metadata", "outcome": "warn",
                                      "note": "Hugging Face could not be reached: " + str(fetched.get("error"))}]
                                    if not repo else []),
        "params_b": g("hf.params_b"), "vram_4bit_gb": g("derived.vram_4bit_gb"),
        "fits_gpu": (None if g("derived.vram_ratio") is None else g("derived.vram_ratio") <= 1.0),
        "download_gb": g("derived.download_gb"), "download_min": g("derived.download_min"),
        "on_disk": on_disk, "gated": g("hf.gated"), "license": g("hf.license"), "downloads": g("hf.downloads"),
        "original_format": ("original weights" if g("derived.prequantized") is False else g("derived.quant_format")),
        "quantization": "4-bit NF4",
        "verification": {"status": "not_verified",
                         "note": "a 4-bit test-load by the admin comes in the next phase"},
        "source_url": g("hf.source_url") or f"https://huggingface.co/{entry['id']}",
        "metadata_status": fetched.get("status"), "fetched_at": fetched.get("fetched_at"),
        "audit": ev["audit"],
    }


def build_catalog(*, gpu_vram_mb: Optional[float] = None, fetcher: Optional[Callable] = None, cache: Any = None,
                  token: Optional[str] = None, enabled: Optional[bool] = None,
                  on_disk: Optional[Callable[[str], bool]] = None, native: Optional[Callable] = None,
                  env: Optional[dict] = None, catalog_path=None, rules_path=None) -> dict[str, Any]:
    from . import runtime
    from ..session.benchmark import canonical_models
    from ..session.stage3 import METADATA_ENV

    rules, entries = load_rules(rules_path), load_catalog(catalog_path)
    env = env if env is not None else server_env(gpu_vram_mb)
    if enabled is None:
        enabled = os.environ.get(METADATA_ENV, "on").strip().lower() not in ("off", "0", "false", "no")
    disk = on_disk or runtime.model_on_disk

    def one(e):
        f = (fetch_repo(e["id"], fetcher=fetcher, cache=cache, token=token) if enabled else
             {"status": "unavailable", "repo": None, "error": f"disabled ({METADATA_ENV}=off)"})
        return assess(e, f, env, rules, on_disk=disk(e["id"]), native=native)

    with ThreadPoolExecutor(max_workers=4) as pool:
        extra = list(pool.map(one, entries))
    return {
        "framing": FRAMING, "quantization": QUANTIZATION, "rulebase": rules["source"],
        "server": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in env.items()},
        "study": [{"id": m, "validated": True, "on_disk": disk(m)} for m in canonical_models()],
        "extra": extra,
        "counts": {s: sum(1 for x in extra if x["status"] == s) for s in ("ok", "warning", "blocked", "unknown")},
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class MemoryCache:
    """Process-local cache for repo records (24 h TTL is applied by fetch_repo). Deliberately not
    the app DB: catalog lookups never write to a results database."""

    def __init__(self):
        import threading
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            return self._d.get(key)

    def set(self, key: str, entry: dict) -> None:
        with self._lock:
            self._d[key] = entry


def register_models(app, gpu_vram_mb: Optional[Callable[[], Any]] = None) -> None:
    """GET /api/models/catalog — study models + extra models with their suitability."""
    from flask import jsonify

    from .hf_metadata import token_from_env

    cache = MemoryCache()

    @app.route("/api/models/catalog", methods=["GET"])
    def models_catalog():
        try:
            out = build_catalog(gpu_vram_mb=gpu_vram_mb() if gpu_vram_mb else None, cache=cache,
                                token=token_from_env())
        except CatalogError as e:
            return jsonify({"error": str(e), "code": "server_config"}), 500
        return jsonify(out)
