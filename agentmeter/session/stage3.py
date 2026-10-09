"""Rule-base STAGE 3: a recommendation that combines measured metrics with external
Hugging Face metadata (configs/recommendation_rules.yaml).

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

    stage 1  flow selection      configs/flow_rules.yaml    -> agentmeter/ingest/rules.py
    stage 2  measured decision   config.yaml scoring (SAW, tiers) + the head-to-head
                                 dominance/tie/significance rules -> session/scoring.py,
                                 session/relative.py, session/brief.py
    stage 3  context             configs/recommendation_rules.yaml -> THIS module

Stage 3 runs AFTER the session is scored. It gets a deep copy of the scored payload,
so it cannot change any score, rank, SAW value or the efficiency verdict. It only adds
two keys to session_results.json:

    model_context           per model: the Hugging Face fields used, the source URL and
                            fetched_at, or status "unavailable" with the reason
    recommendation_stage3   the measured verdict FIRST, then the notes of the rules
                            that fired (rule id + sources), and an audit of every rule
                            (fired / not_fired / no_data) for the PDF "Rules fired" table

Fetching metadata never fails a job: offline, rate-limited or disabled
(AGENTMETER_HF_METADATA=off) is recorded as status "unavailable" with the reason, and
the metadata rules then report "no_data".
"""
from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..config import PROJECT_ROOT

DEFAULT_RULES = PROJECT_ROOT / "configs" / "recommendation_rules.yaml"
METADATA_ENV = "AGENTMETER_HF_METADATA"          # "off" disables the external fetch
OPS = {"present", "truthy", "eq", "ne", "lt", "le", "gt", "ge", "in", "not_in", "matches"}
_FIELD_ROOTS = ("model", "measured.", "env.", "hf.", "derived.")
_SOURCE_NAMES = {"measured", "huggingface", "session_gpu"}
_TPL = re.compile(r"\{([A-Za-z_][\w.]*)(?::([^{}]+))?\}")


class Stage3RuleError(ValueError):
    """The recommendation rule file is malformed."""


# ---------------------------------------------------------------------------
# rule file
# ---------------------------------------------------------------------------
def load_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    p = Path(path or DEFAULT_RULES)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise Stage3RuleError(f"cannot read {p}: {e}") from e
    consts = data.get("constants") or {}
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise Stage3RuleError(f"{p}: 'rules' must be a non-empty list")
    seen = set()
    for r in rules:
        rid = r.get("id")
        if not rid or rid in seen:
            raise Stage3RuleError(f"{p}: every rule needs a unique id (got {rid!r})")
        seen.add(rid)
        if not r.get("note") or not isinstance(r.get("when"), list) or not r["when"]:
            raise Stage3RuleError(f"{p}: rule {rid}: needs 'when' (list) and 'note'")
        for c in r["when"]:
            f, op = c.get("field", ""), c.get("op")
            if op not in OPS:
                raise Stage3RuleError(f"{p}: rule {rid}: unknown op {op!r} (one of {sorted(OPS)})")
            if not (f == "model" or f.startswith(_FIELD_ROOTS[1:])):
                raise Stage3RuleError(f"{p}: rule {rid}: field {f!r} must start with measured./env./hf./derived.")
            v = c.get("value")
            if isinstance(v, str) and v.startswith("$") and v[1:] not in consts:
                raise Stage3RuleError(f"{p}: rule {rid}: unknown constant {v}")
        bad = set(r.get("sources") or []) - _SOURCE_NAMES
        if bad:
            raise Stage3RuleError(f"{p}: rule {rid}: unknown sources {sorted(bad)}")
    return {"version": data.get("version", 1), "constants": consts, "rules": rules,
            "source": str(p.relative_to(PROJECT_ROOT) if p.is_relative_to(PROJECT_ROOT) else p)}


# ---------------------------------------------------------------------------
# external metadata (Hugging Face Hub)
# ---------------------------------------------------------------------------
_HF_FIELDS = ("gated", "license", "params", "params_b", "downloads", "likes", "last_modified",
              "pipeline_tag", "source_url", "fetched_at")


def fetch_model_context(models: list[str], *, fetcher: Optional[Callable] = None,
                        cache: Any = None, enabled: Optional[bool] = None) -> dict[str, Any]:
    """Hugging Face metadata for the session's models. Never raises."""
    from ..server import hf_metadata

    if enabled is None:
        enabled = os.environ.get(METADATA_ENV, "on").strip().lower() not in ("off", "0", "false", "no")
    base = {"source": "Hugging Face Hub API", "api": hf_metadata.HF_API_BASE + "<model id>",
            "purpose": "context only — never used for any score, rank, SAW value or verdict"}
    if not enabled:
        return {**base, "status": "unavailable", "reason": f"disabled ({METADATA_ENV}=off)",
                "models": {m: {"status": "unavailable", "reason": f"disabled ({METADATA_ENV}=off)",
                               "source_url": f"https://huggingface.co/{m}", "fetched_at": None}
                           for m in models}}
    if cache is None:
        try:
            from ..db import appdb
            cache = hf_metadata.make_store_cache(appdb.AppStore(appdb.DEFAULT_APP_DB))
        except Exception:  # noqa: BLE001 — the cache is optional
            cache = None
    out: dict[str, dict] = {}
    for m in models:
        try:
            md = hf_metadata.get_metadata(m, fetcher=fetcher, cache=cache, token=hf_metadata.token_from_env())
        except Exception as e:  # noqa: BLE001 — belt and braces: get_metadata already never raises
            md = {"status": "unavailable", "error": str(e)}
        rec = {"status": md.get("status", "unavailable")}
        if rec["status"] in ("ok", "stale"):
            rec.update({k: md.get(k) for k in _HF_FIELDS})
            if rec["status"] == "stale":
                rec["reason"] = f"live fetch failed, using the cached copy ({md.get('error')})"
        else:
            rec.update(reason=md.get("error") or "unavailable", source_url=f"https://huggingface.co/{m}",
                       fetched_at=None)
        out[m] = rec
    states = {r["status"] for r in out.values()}
    status = "ok" if states == {"ok"} else ("unavailable" if states == {"unavailable"} else "partial")
    return {**base, "status": status, "models": out}


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------
def _parse_time(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _facts(payload: dict, context: dict) -> list[dict[str, Any]]:
    rel = payload.get("relative_comparison") or {}
    ev = rel.get("efficiency_verdict") or {}
    verdict = ev.get("verdict") if rel else "single_model"
    env = payload.get("environment") or {}
    models = [m["model"] for m in payload.get("per_model") or []]
    hf_all = (context or {}).get("models") or {}
    out = []
    for pm in payload.get("per_model") or []:
        m = pm["model"]
        e2e = (pm.get("efficiency") or {}).get("end_to_end") or {}
        hf = dict(hf_all.get(m) or {"status": "unavailable"})
        f: dict[str, Any] = {
            "model": m,
            "measured.peak_vram_mb": e2e.get("mean_peak_vram_mb"),
            "measured.mean_latency_s": e2e.get("mean_latency_s"),
            "measured.verdict": verdict,
            "measured.is_efficiency_winner": bool(verdict == "more_efficient" and ev.get("model") == m),
            "env.gpu_name": env.get("gpu_name"), "env.gpu_vram_total_mb": env.get("gpu_vram_total_mb"),
            "hf.status": hf.get("status"),
        }
        for k in _HF_FIELDS:
            f[f"hf.{k}"] = hf.get(k)
        if f["measured.peak_vram_mb"] is not None and f["env.gpu_vram_total_mb"]:
            head = float(f["env.gpu_vram_total_mb"]) - float(f["measured.peak_vram_mb"])
            f["derived.vram_headroom_mb"] = head
            f["derived.vram_headroom_pct"] = 100.0 * head / float(f["env.gpu_vram_total_mb"])
        t_mod, t_now = _parse_time(hf.get("last_modified")), _parse_time(hf.get("fetched_at"))
        if t_mod and t_now:
            f["derived.age_months"] = (t_now - t_mod).days / 30.44
        others = [o for o in models if o != m]
        if len(others) == 1:
            op = (hf_all.get(others[0]) or {}).get("params_b")
            f["derived.other_params_b"] = op
            if op is not None and hf.get("params_b") is not None:
                f["derived.smaller_than_other"] = float(hf["params_b"]) < float(op)
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def _value(v: Any, consts: dict) -> Any:
    return consts[v[1:]] if isinstance(v, str) and v.startswith("$") else v


def _check(c: dict, facts: dict, consts: dict) -> tuple[Optional[bool], str]:
    """(True|False, text) or (None, why) when the field has no data."""
    field, op, val = c["field"], c["op"], _value(c.get("value"), consts)
    have = facts.get(field)
    text = f"{field} {op}" + (f" {c.get('value')}" if "value" in c else "")
    if have is None:                     # "present" too: a missing reading is no data, not a "no"
        return None, f"no data for {field}"
    try:
        ok = {"present": lambda: True, "truthy": lambda: bool(have), "eq": lambda: have == val, "ne": lambda: have != val,
              "lt": lambda: float(have) < float(val), "le": lambda: float(have) <= float(val),
              "gt": lambda: float(have) > float(val), "ge": lambda: float(have) >= float(val),
              "in": lambda: have in (val or []), "not_in": lambda: have not in (val or []),
              "matches": lambda: re.search(str(val), str(have), re.I) is not None}[op]()
    except (TypeError, ValueError):
        return None, f"cannot compare {field}={have!r} with {val!r}"
    return ok, text


def render(template: str, facts: dict) -> str:
    def sub(mt):
        key, spec = mt.group(1), mt.group(2)
        v = facts.get(key)
        if v is None:
            return "unknown"
        if spec:
            try:
                return format(v, spec)
            except (TypeError, ValueError):
                return str(v)
        return str(v)
    return _TPL.sub(sub, template)


def _verdict_line(payload: dict) -> dict[str, Any]:
    rel = payload.get("relative_comparison")
    if rel:
        ev = rel.get("efficiency_verdict") or {}
        return {"kind": "head_to_head", "verdict": ev.get("verdict"), "model": ev.get("model"),
                "text": rel.get("verdict"), "source": "measured"}
    cmp_ = payload.get("comparison") or {}
    return {"kind": "single_model", "verdict": "single_model", "model": None,
            "text": cmp_.get("statement") or "Single-model run — no head-to-head comparison.",
            "source": "measured"}


def evaluate(payload: dict, context: dict, rules: Optional[dict] = None) -> dict[str, Any]:
    """Stage-3 block for a scored payload. Reads a deep copy; returns a new dict."""
    rb = rules or load_rules()
    snap = copy.deepcopy(payload)
    facts_all = _facts(snap, context)
    audit, notes = [], []
    for r in rb["rules"]:
        cond_text = " AND ".join(f"{c['field']} {c['op']}" + (f" {c['value']}" if "value" in c else "")
                                 for c in r["when"])
        for facts in facts_all:
            results = [_check(c, facts, rb["constants"]) for c in r["when"]]
            if any(ok is None for ok, _ in results):
                result, detail = "no_data", "; ".join(t for ok, t in results if ok is None)
            elif all(ok for ok, _ in results):
                result, detail = "fired", "all conditions hold"
            else:
                result, detail = "not_fired", "; ".join(f"not ({t})" for ok, t in results if ok is False)
            hf_used = "huggingface" in (r.get("sources") or [])
            row = {"rule": r["id"], "description": r.get("description", ""), "model": facts["model"],
                   "condition": cond_text, "result": result, "detail": detail,
                   "sources": list(r.get("sources") or []),
                   "source_url": facts.get("hf.source_url") if hf_used else None,
                   "fetched_at": facts.get("hf.fetched_at") if hf_used else None}
            if result == "fired":
                row["note"] = render(r["note"], facts)
                notes.append({k: row[k] for k in ("rule", "model", "note", "sources", "source_url", "fetched_at")})
            audit.append(row)
    return {
        "stage": 3, "rulebase": rb["source"], "version": rb["version"],
        "framing": ("Context for the measured result, from external Hugging Face metadata. It never "
                    "changes a score, rank, SAW value or the efficiency verdict."),
        "verdict": _verdict_line(snap),
        "notes": notes,
        "rules": audit,
        "metadata_status": (context or {}).get("status", "unavailable"),
        "fired": sorted({n["rule"] for n in notes}),
    }


def add_stage3(payload: dict[str, Any], *, context: Optional[dict] = None,
               fetcher: Optional[Callable] = None, rules_path: Optional[str | Path] = None) -> dict[str, Any]:
    """Attach model_context + recommendation_stage3 to a scored payload (in place) and
    return it. Any failure here is recorded, never raised: a job never fails on stage 3."""
    models = [m["model"] for m in payload.get("per_model") or []]
    try:
        ctx = context if context is not None else fetch_model_context(models, fetcher=fetcher)
    except Exception as e:  # noqa: BLE001
        ctx = {"status": "unavailable", "reason": f"{type(e).__name__}: {e}", "models": {}}
    try:
        block = evaluate(payload, ctx, load_rules(rules_path))
    except Exception as e:  # noqa: BLE001
        block = {"stage": 3, "error": f"{type(e).__name__}: {e}", "notes": [], "rules": [],
                 "verdict": _verdict_line(payload), "metadata_status": ctx.get("status")}
    payload["model_context"] = ctx
    payload["recommendation_stage3"] = block
    return payload
