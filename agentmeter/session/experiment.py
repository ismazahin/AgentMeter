"""Experiments: one upload benchmarked at several model sizes (precision) and pipeline sizes
(number of agents), compared in charts.

A plain benchmark is the one-cell case: 4-bit, 4 agents. Each other combination of a precision and
an agent count is its own session (a "cell") on the same prepared flows, the same IDS threshold, the
same GPU and the same settings, differing only in those two factors. Precision and the pipeline are
part of the settings fingerprint, so cells are separate setups and never pooled with each other.

    precision  4bit (bitsandbytes NF4, double quantization, fp16 compute: the standard)
               8bit (bitsandbytes LLM.int8)
               16bit (no quantization: the weights in fp16)
    agents     1, 2, 3, 4, 6, 8, 10 (pipeline/agents.py LADDERS)
"""
from __future__ import annotations

import copy
import uuid
from typing import Any, Optional

from ..pipeline.agents import AGENT_COUNTS, STANDARD_AGENTS, ladder

STANDARD_PRECISION = "4bit"
PRECISIONS: dict[str, dict[str, Any]] = {
    "4bit": {"load_in_4bit": True, "load_in_8bit": False, "bnb_4bit_quant_type": "nf4",
             "bnb_4bit_use_double_quant": True, "bnb_4bit_compute_dtype": "float16"},
    "8bit": {"load_in_4bit": False, "load_in_8bit": True},
    "16bit": {"load_in_4bit": False, "load_in_8bit": False},
}
PRECISION_LABEL = {"4bit": "4-bit", "8bit": "8-bit", "16bit": "16-bit"}
# GPU memory per billion parameters for the weights (bytes per parameter), plus a fixed overhead
# (CUDA context, allocator) and one flow's working memory. 4-bit matches configs/model_rules.yaml.
GB_PER_B = {"4bit": 0.55, "8bit": 1.05, "16bit": 2.0}
OVERHEAD_GB = 1.5
WORKING_GB = 1.0
MAX_CELLS = 12


class ExperimentError(ValueError):
    def __init__(self, message: str, code: str = "bad_experiment"):
        super().__init__(message)
        self.code = code


def normalize_precisions(value: Any) -> list[str]:
    vals = [value] if isinstance(value, str) else list(value or [STANDARD_PRECISION])
    out = []
    for v in vals:
        k = str(v).strip().lower().replace("-", "").replace(" ", "")
        if k not in PRECISIONS:
            raise ExperimentError(f"unknown model size {v!r} (4bit | 8bit | 16bit)")
        if k not in out:
            out.append(k)
    return sorted(out, key=list(PRECISIONS).index)


def normalize_agents(value: Any) -> list[int]:
    vals = [value] if isinstance(value, (int, str)) else list(value or [STANDARD_AGENTS])
    out = []
    for v in vals:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ExperimentError(f"number of agents must be a number, got {v!r}") from None
        if n not in AGENT_COUNTS:
            raise ExperimentError(f"no {n}-agent pipeline; the sizes are {', '.join(map(str, AGENT_COUNTS))}")
        if n not in out:
            out.append(n)
    return sorted(out)


def apply_variant(data: dict[str, Any], precision: Optional[str] = None, agents: Optional[int] = None) -> dict[str, Any]:
    """Set a session config's quantization and pipeline for one cell (in place; returns data)."""
    p = normalize_precisions(precision or STANDARD_PRECISION)[0]
    n = normalize_agents(agents or STANDARD_AGENTS)[0]
    hf = data.setdefault("model", {}).setdefault("hf", {})
    hf["quantization"] = copy.deepcopy(PRECISIONS[p])
    data.setdefault("pipeline", {})["agents"] = ladder(n)
    return data


def vram_need_gb(params_b: Optional[float], precision: str) -> Optional[float]:
    """Rough GPU memory a model needs at this precision (weights + overhead + one flow)."""
    if params_b is None:
        return None
    return round(float(params_b) * GB_PER_B[precision] + OVERHEAD_GB + WORKING_GB, 1)


def plan(models: list[str], precisions: Any, agents: Any) -> dict[str, Any]:
    """The cells of an experiment, grouped into jobs: one job per (precision, agents) holding every
    model, ordered by precision then agent count (a model's cells of one precision run back to back)."""
    ps, ns = normalize_precisions(precisions), normalize_agents(agents)
    models = list(models)
    cells = [{"model": m, "precision": p, "agents": n} for p in ps for n in ns for m in models]
    if len(cells) > MAX_CELLS:
        raise ExperimentError(f"{len(cells)} runs is more than the {MAX_CELLS} an experiment may have; "
                              "untick a model, a size or a number of agents", "too_many_runs")
    jobs = [{"precision": p, "agents": n, "models": models} for p in ps for n in ns]
    return {"id": "exp_" + uuid.uuid4().hex[:16], "precisions": ps, "agents": ns, "models": models,
            "cells": cells, "jobs": jobs, "is_experiment": len(jobs) > 1}


def label(precision: Optional[str], agents: Optional[int]) -> str:
    return f"{PRECISION_LABEL.get(precision or STANDARD_PRECISION, precision)}, {agents or STANDARD_AGENTS} agents"


def variant_block(precision: Optional[str], agents: Optional[int]) -> dict[str, Any]:
    """What a session's results record about its cell: model size and pipeline size."""
    p = normalize_precisions(precision or STANDARD_PRECISION)[0]
    n = normalize_agents(agents or STANDARD_AGENTS)[0]
    return {"precision": p, "agents": n, "pipeline": ladder(n), "label": label(p, n),
            "standard": p == STANDARD_PRECISION and n == STANDARD_AGENTS}
