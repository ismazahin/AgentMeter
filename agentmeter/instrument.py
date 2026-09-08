"""Phase 3 — per-agent instrumentation layer (CORE CONTRIBUTION).

Wraps each agent node (via Pipeline's node_hook seam) and records, around that
node's single inference call:
  - wall_time_s   : time.perf_counter() delta over the agent call
  - ttft_s        : time-to-first-token (from the provider; needs streaming)
  - vram_peak_mb  : torch.cuda peak-allocated delta for the node (reset before)
  - input_tokens, output_tokens : from the provider's GenerationResult

Because agents run sequentially, each node's readings are clean — no other
agent is active. Readings are keyed by (scenario_id, model, agent_name).

VRAM is measured only when a CUDA device is present. On CPU/mock it is recorded
as None (never fabricated). The same code lights up unchanged on the GPU host.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class AgentMetrics:
    scenario_id: str
    model: str
    agent_name: str
    wall_time_s: float
    ttft_s: Optional[float]
    vram_peak_mb: Optional[float]
    input_tokens: int
    output_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GpuProbe:
    """VRAM measurement via torch.cuda. Degrades to a no-op without CUDA."""

    def __init__(self) -> None:
        self.available = False
        self._torch = None
        self._baseline = 0
        self.device = None
        try:
            import torch  # noqa: WPS433

            if torch.cuda.is_available():
                self._torch = torch
                self.device = torch.cuda.current_device()
                self.available = True
        except Exception:
            self.available = False

    def reset(self) -> None:
        """Record the resident baseline and reset the peak counter before a node."""
        if not self.available:
            return
        self._torch.cuda.synchronize(self.device)
        self._baseline = self._torch.cuda.memory_allocated(self.device)
        self._torch.cuda.reset_peak_memory_stats(self.device)

    def read_peak_delta_mb(self) -> Optional[float]:
        """Peak-allocated bytes above the pre-node baseline, in MB (None on CPU)."""
        if not self.available:
            return None
        self._torch.cuda.synchronize(self.device)
        peak = self._torch.cuda.max_memory_allocated(self.device)
        return max(0.0, float(peak - self._baseline)) / (1024 * 1024)

    def total_used_mb(self) -> Optional[float]:
        """Whole-device allocated VRAM in MB via the torch allocator."""
        if not self.available:
            return None
        self._torch.cuda.synchronize(self.device)
        return float(self._torch.cuda.memory_allocated(self.device)) / (1024 * 1024)

    def pynvml_used_mb(self) -> Optional[float]:
        """Device-level used VRAM in MB via NVML.

        Captures memory the torch allocator does not see (e.g. bitsandbytes
        quantized weights), so it is the honest figure for total footprint.
        """
        if not self.available:
            return None
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(self.device or 0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return float(info.used) / (1024 * 1024)
        except Exception:
            return None


class MetricsCollector:
    """Accumulates AgentMetrics across a run."""

    def __init__(self) -> None:
        self.rows: list[AgentMetrics] = []

    def add(self, m: AgentMetrics) -> None:
        self.rows.append(m)

    # --- aggregation ----------------------------------------------------
    def per_agent_summary(self) -> dict[str, dict[str, float]]:
        """Mean per-agent cost across all scenarios."""
        agg: dict[str, dict[str, list]] = {}
        for r in self.rows:
            a = agg.setdefault(
                r.agent_name,
                {"wall": [], "ttft": [], "vram": [], "in": [], "out": []},
            )
            a["wall"].append(r.wall_time_s)
            if r.ttft_s is not None:
                a["ttft"].append(r.ttft_s)
            if r.vram_peak_mb is not None:
                a["vram"].append(r.vram_peak_mb)
            a["in"].append(r.input_tokens)
            a["out"].append(r.output_tokens)

        def mean(xs: list) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        out: dict[str, dict[str, float]] = {}
        for name, a in agg.items():
            out[name] = {
                "mean_wall_s": mean(a["wall"]),
                "mean_ttft_s": mean(a["ttft"]) if a["ttft"] else float("nan"),
                "mean_vram_mb": mean(a["vram"]) if a["vram"] else float("nan"),
                "mean_input_tokens": mean(a["in"]),
                "mean_output_tokens": mean(a["out"]),
                "n": len(a["wall"]),
            }
        return out

    def per_scenario_totals(self) -> dict[str, dict[str, float]]:
        """Sum of agent costs within each scenario."""
        out: dict[str, dict[str, float]] = {}
        for r in self.rows:
            t = out.setdefault(
                r.scenario_id, {"wall_s": 0.0, "input_tokens": 0, "output_tokens": 0, "vram_mb": 0.0}
            )
            t["wall_s"] += r.wall_time_s
            t["input_tokens"] += r.input_tokens
            t["output_tokens"] += r.output_tokens
            if r.vram_peak_mb is not None:
                t["vram_mb"] = max(t["vram_mb"], r.vram_peak_mb)  # peak, not sum
        return out

    def mean_scenario_wall_s(self) -> float:
        totals = self.per_scenario_totals()
        if not totals:
            return 0.0
        return sum(t["wall_s"] for t in totals.values()) / len(totals)


def make_instrumented_hook(collector: MetricsCollector, model_label: str, gpu: GpuProbe):
    """Return a Pipeline node_hook that measures each agent call."""

    def hook(name, agent_fn, state, provider, config) -> dict:
        gpu.reset()
        t0 = time.perf_counter()
        update = agent_fn(state, provider, config)
        wall = time.perf_counter() - t0
        vram = gpu.read_peak_delta_mb()

        res = update.get("_last_result")
        collector.add(
            AgentMetrics(
                scenario_id=state.get("scenario_id", "?"),
                model=model_label,
                agent_name=name,
                wall_time_s=wall,
                ttft_s=getattr(res, "ttft_s", None),
                vram_peak_mb=vram,
                input_tokens=getattr(res, "input_tokens", 0),
                output_tokens=getattr(res, "output_tokens", 0),
            )
        )
        return update

    return hook
