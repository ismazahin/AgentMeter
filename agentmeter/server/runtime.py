"""Phase E: what this backend RUNS ON, decided once at startup and recorded everywhere.

Provider ("real" = Hugging Face models on this machine's GPU, 4-bit NF4; "mock" =
deterministic heuristic, not a language model) is chosen ONCE when the server
starts, in this order:

    --provider real|mock|auto   (scripts/pull_eval_server.py)
    AGENTMETER_SERVICE_PROVIDER = real|hf|mock|auto
    auto (default): real when CUDA is visible, else mock (local development).

There is no silent fallback: in REAL mode `preflight()` must pass before the
server starts (CUDA GPU, bitsandbytes, every study model already on local disk,
an access passcode) — otherwise the server refuses to start with the reason. A
real server never runs a job on the mock provider and a mock server never claims
real numbers; every job, session result, PDF and prepared-set manifest carries
`environment()` (provider, GPU name, VRAM, driver, CUDA, library versions).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..config import PROJECT_ROOT, load_config

REAL_BASE_CONFIG = PROJECT_ROOT / "configs" / "run_full_l4.yaml"
PROVIDER_ENV = "AGENTMETER_SERVICE_PROVIDER"
_ALIASES = {"real": "real", "hf": "real", "mock": "mock", "auto": "auto", "": "auto"}


class RealModeError(RuntimeError):
    """Real mode was requested but this machine cannot provide it."""


def requested_mode(cli: Optional[str] = None) -> str:
    raw = (cli or os.environ.get(PROVIDER_ENV) or "auto").strip().lower()
    if raw not in _ALIASES:
        raise RealModeError(f"unknown provider {raw!r}: use real, mock or auto")
    return _ALIASES[raw]


def resolve_mode(cli: Optional[str] = None, gpu_available: Optional[bool] = None) -> str:
    """'real' | 'mock'. auto = real iff CUDA is visible (local development only)."""
    mode = requested_mode(cli)
    if mode != "auto":
        return mode
    if gpu_available is None:
        gpu_available = bool(gpu_info().get("cuda_available"))
    return "real" if gpu_available else "mock"


def job_provider(mode: str) -> str:
    """The model.provider every job on this server uses."""
    return "hf" if mode == "real" else "mock"


# ---------------------------------------------------------------------------
# facts about this machine
# ---------------------------------------------------------------------------
def _nvidia_smi() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip().splitlines()
        name, mem, drv = [x.strip() for x in out[0].split(",")[:3]]
        top = subprocess.run([exe], capture_output=True, text=True, timeout=10).stdout
        cuda = next((ln.split("CUDA Version:")[1].split("|")[0].strip() for ln in top.splitlines()
                     if "CUDA Version:" in ln), None)
        return {"name": name, "vram_total_mb": int(float(mem)), "driver_version": drv,
                "cuda_driver_version": cuda, "count": len(out)}
    except Exception:  # noqa: BLE001 — informational
        return {}


@lru_cache(maxsize=1)
def gpu_info() -> dict[str, Any]:
    """GPU facts (cached: they do not change while the server runs)."""
    info: dict[str, Any] = {"cuda_available": False}
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda_runtime_version"] = torch.version.cuda
        if torch.cuda.is_available():
            i = torch.cuda.current_device()
            p = torch.cuda.get_device_properties(i)
            info.update(cuda_available=True, name=torch.cuda.get_device_name(i),
                        vram_total_mb=int(p.total_memory / 1024 ** 2), count=torch.cuda.device_count())
    except Exception:  # noqa: BLE001 — torch missing / broken driver: no GPU
        pass
    smi = _nvidia_smi()
    for k, v in smi.items():
        info.setdefault(k, v)
    info["driver_version"] = smi.get("driver_version")
    info["cuda_driver_version"] = smi.get("cuda_driver_version")
    return info


def _version(dist: str) -> Optional[str]:
    from importlib import metadata

    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def study_models(base_config: Optional[Path] = None) -> list[str]:
    cfg = load_config(base_config or REAL_BASE_CONFIG)
    return [str(m) for m in (cfg.get("run.models") or [])]


def _hub_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    return home / "hub"


def model_on_disk(model_id: str, cache: Optional[Path] = None) -> bool:
    """A model counts as local when its snapshot has a config and weight files."""
    d = (cache or _hub_cache()) / ("models--" + model_id.replace("/", "--")) / "snapshots"
    if not d.is_dir():
        return False
    for snap in d.iterdir():
        if (snap / "config.json").exists() and (any(snap.glob("*.safetensors")) or any(snap.glob("*.bin"))):
            return True
    return False


def models_local(models: Optional[list[str]] = None) -> dict[str, bool]:
    return {m: model_on_disk(m) for m in (models if models is not None else study_models())}


def environment(mode: str) -> dict[str, Any]:
    """The record stamped into every job, session result, PDF and manifest."""
    g = gpu_info() if mode == "real" else {}
    return {
        "provider": mode,
        "provider_note": ("real models on this GPU, 4-bit NF4 (bitsandbytes), one model at a time"
                          if mode == "real" else
                          "MOCK provider: deterministic heuristic, not a language model — not a measurement"),
        "gpu_name": g.get("name"), "gpu_count": g.get("count"), "gpu_vram_total_mb": g.get("vram_total_mb"),
        "driver_version": g.get("driver_version"), "cuda_driver_version": g.get("cuda_driver_version"),
        "cuda_runtime_version": g.get("cuda_runtime_version"),
        "torch": g.get("torch") or _version("torch"), "transformers": _version("transformers"),
        "bitsandbytes": _version("bitsandbytes"), "python": platform.python_version(),
        "host": ("vast.ai instance " + os.environ.get("VAST_CONTAINERLABEL", os.environ.get("CONTAINER_ID", "")))
                if (os.environ.get("VAST_CONTAINERLABEL") or os.environ.get("CONTAINER_ID")) else platform.node(),
    }


# ---------------------------------------------------------------------------
# real-mode preflight: refuse to start instead of falling back
# ---------------------------------------------------------------------------
def preflight(base_config: Optional[Path] = None, *, require_passcode: bool = True) -> dict[str, Any]:
    """Raise RealModeError (with every problem listed) unless real mode can run."""
    problems: list[str] = []
    g = gpu_info()
    if not g.get("cuda_available"):
        problems.append("no CUDA GPU is visible to PyTorch (torch.cuda.is_available() is False"
                        + ("" if g.get("torch") else "; torch is not installed") + ")")
    if _version("bitsandbytes") is None:
        problems.append("bitsandbytes is not installed (needed for 4-bit NF4): pip install -r requirements-gpu.txt")
    cfg = load_config(base_config or REAL_BASE_CONFIG)
    if cfg.get("model.provider") != "hf":
        problems.append(f"base config {cfg.path} does not use model.provider: hf")
    q = cfg.get("model.hf.quantization") or {}
    if not (q.get("load_in_4bit") and str(q.get("bnb_4bit_quant_type", "nf4")) == "nf4"):
        problems.append("base config is not uniform 4-bit NF4")
    missing = [m for m, ok in models_local(study_models(base_config)).items() if not ok]
    if missing:
        problems.append("study models not on local disk (run scripts/vast_models.py with HF_TOKEN): "
                        + ", ".join(missing))
    if require_passcode and not os.environ.get("AGENTMETER_PASSCODE") \
            and os.environ.get("AGENTMETER_INSECURE_NO_PASSCODE") != "1":
        problems.append("AGENTMETER_PASSCODE is not set: a public GPU endpoint must not run jobs for anyone")
    if problems:
        raise RealModeError("refusing to start in REAL mode (no fallback to mock):\n  - "
                            + "\n  - ".join(problems))
    return environment("real")
