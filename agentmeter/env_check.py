"""Phase 0 environment check.

Confirms the environment is coherent WITHOUT requiring a GPU. It reports:
  - Python version
  - which dependencies are importable (CPU set vs GPU set)
  - torch / CUDA status (informational at Phase 0)
  - that config.yaml loads and validates
  - which run mode / model provider is configured

Enforcement rule (from the build plan): a GPU is only *required* when
run.require_gpu is true (Phase 5 pilot). In that case, if torch.cuda is
unavailable this check FAILS loudly rather than silently falling back to CPU.
Below that, missing torch/GPU is reported but not fatal.
"""
from __future__ import annotations

import importlib
import platform
import sys
from dataclasses import dataclass, field

from .config import Config, load_config

# (module_name, human_label, phase_group)
CPU_DEPS = [
    ("yaml", "PyYAML", "0-3"),
    ("pandas", "pandas", "1+"),
    ("numpy", "NumPy", "3+"),
    ("scipy", "SciPy", "9+"),
    ("langgraph", "LangGraph", "2+"),
]
GPU_DEPS = [
    ("torch", "PyTorch", "4-5"),
    ("transformers", "Transformers", "4-5"),
    ("huggingface_hub", "huggingface_hub", "4-5"),
    ("pynvml", "pynvml", "5"),
]


@dataclass
class DepStatus:
    module: str
    label: str
    phase: str
    installed: bool
    version: str | None = None


@dataclass
class EnvReport:
    python_version: str
    platform: str
    cpu_deps: list[DepStatus] = field(default_factory=list)
    gpu_deps: list[DepStatus] = field(default_factory=list)
    torch_available: bool = False
    cuda_available: bool = False
    cuda_device_name: str | None = None
    cuda_vram_total_mb: float | None = None
    config_ok: bool = False
    config_error: str | None = None
    run_mode: str | None = None
    model_provider: str | None = None
    require_gpu: bool = False
    fatal: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fatal


def _probe(deps) -> list[DepStatus]:
    out = []
    for module, label, phase in deps:
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", None)
            out.append(DepStatus(module, label, phase, True, version))
        except Exception:
            out.append(DepStatus(module, label, phase, False, None))
    return out


def check_environment(config: Config | None = None) -> EnvReport:
    if config is None:
        try:
            config = load_config()
        except Exception as exc:  # config problems are reported, not raised
            report = EnvReport(
                python_version=platform.python_version(),
                platform=platform.platform(),
            )
            report.config_ok = False
            report.config_error = str(exc)
            report.fatal.append(f"config.yaml failed to load/validate: {exc}")
            report.cpu_deps = _probe(CPU_DEPS)
            report.gpu_deps = _probe(GPU_DEPS)
            return report

    report = EnvReport(
        python_version=platform.python_version(),
        platform=platform.platform(),
    )
    report.config_ok = True
    report.run_mode = config.get("run.mode")
    report.model_provider = config.get("model.provider")
    report.require_gpu = bool(config.get("run.require_gpu", False))

    report.cpu_deps = _probe(CPU_DEPS)
    report.gpu_deps = _probe(GPU_DEPS)

    # torch / CUDA probe (informational unless require_gpu).
    try:
        import torch  # noqa: WPS433

        report.torch_available = True
        report.cuda_available = bool(torch.cuda.is_available())
        if report.cuda_available:
            idx = torch.cuda.current_device()
            report.cuda_device_name = torch.cuda.get_device_name(idx)
            props = torch.cuda.get_device_properties(idx)
            report.cuda_vram_total_mb = props.total_memory / (1024 * 1024)
    except Exception:
        report.torch_available = False
        report.cuda_available = False

    # Enforcement: GPU required means no silent CPU fallback.
    if report.require_gpu and not report.cuda_available:
        report.fatal.append(
            "run.require_gpu is true but torch.cuda is NOT available. "
            "VRAM measurement is a core requirement — refusing to fall back to CPU. "
            "Run this on the GPU host (HF Space, 1x L4) with GPU torch installed."
        )

    return report


def format_report(report: EnvReport) -> str:
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append("  AgentMeter — Phase 0 Environment Check")
    lines.append("=" * 62)
    lines.append(f"Python           : {report.python_version}")
    lines.append(f"Platform         : {report.platform}")
    lines.append("")

    if report.config_ok:
        lines.append(f"config.yaml      : OK")
        lines.append(f"  run.mode       : {report.run_mode}")
        lines.append(f"  model.provider : {report.model_provider}")
        lines.append(f"  require_gpu    : {report.require_gpu}")
    else:
        lines.append(f"config.yaml      : FAILED — {report.config_error}")
    lines.append("")

    lines.append("CPU / mock dependencies (Phases 0-3):")
    for d in report.cpu_deps:
        mark = "OK " if d.installed else "-- "
        ver = f" ({d.version})" if d.version else ""
        lines.append(f"  [{mark}] {d.label}{ver}  <phase {d.phase}>")
    lines.append("")

    lines.append("GPU / Hugging Face dependencies (Phases 4-5):")
    for d in report.gpu_deps:
        mark = "OK " if d.installed else "-- "
        ver = f" ({d.version})" if d.version else ""
        lines.append(f"  [{mark}] {d.label}{ver}  <phase {d.phase}>")
    lines.append("")

    lines.append("GPU / CUDA:")
    lines.append(f"  torch available: {report.torch_available}")
    lines.append(f"  cuda available : {report.cuda_available}")
    if report.cuda_available:
        lines.append(f"  device         : {report.cuda_device_name}")
        if report.cuda_vram_total_mb:
            lines.append(f"  total VRAM     : {report.cuda_vram_total_mb:,.0f} MB")
    else:
        lines.append("  (No GPU — expected for Phases 0-3. Required at Phase 5.)")
    lines.append("")

    if report.ok:
        lines.append("RESULT: OK — environment is coherent for the current phase.")
    else:
        lines.append("RESULT: STOP — the following must be resolved:")
        for f in report.fatal:
            lines.append(f"  * {f}")
    lines.append("=" * 62)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    report = check_environment()
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
