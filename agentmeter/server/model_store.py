"""Model weights on this GPU server: access check, download, download-on-first-use.

AgentMeter MEASURES LLM resource efficiency; it is not a threat-detection product.

The 5 study models are downloaded once per rental at start-up (scripts/vast_up.sh →
scripts/vast_models.py), after which the server runs OFFLINE (HF_HUB_OFFLINE=1). An extra
model from configs/model_catalog.yaml is downloaded the first time a benchmark uses it:

  ensure_job_models(job, report)   called by the job manager before the benchmark starts
                                   (real GPU only). For every model not yet on disk:
                                   free-disk check → download in a CHILD process with
                                   HF_HUB_OFFLINE=0 (the server itself stays offline) →
                                   progress = bytes in the model's cache folder vs the size
                                   from the Hub → "on disk" check. The download process is
                                   registered with the runner, so Stop terminates it.

Only the original weights a benchmark loads are fetched (safetensors + config/tokenizer;
DOWNLOAD_PATTERNS). AgentMeter quantizes them to 4-bit NF4 at load, like the study models.
Download time is never part of any measurement: it happens before the benchmark starts.

    python -m agentmeter.server.model_store download <model id>     (child process: download)
    python -m agentmeter.server.model_store verify <model id>       (child process: 4-bit test-load)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .model_catalog import DOWNLOAD_IGNORE, DOWNLOAD_PATTERNS, MemoryCache, _wanted, fetch_repo

DISK_MARGIN_BYTES = 2 * 1024 ** 3      # keep at least this much free after a download
POLL_S = 1.0
_SIZES = MemoryCache()                 # repo records for the expected download size


class ModelDownloadError(RuntimeError):
    def __init__(self, message: str, code: str = "download_failed"):
        super().__init__(message)
        self.code = code


def token() -> Optional[str]:
    return (os.environ.get("HF_TOKEN") or "").strip() or None


def cache_dir(model_id: str) -> Path:
    from . import runtime
    return runtime._hub_cache() / ("models--" + model_id.replace("/", "--"))


def cache_bytes(model_id: str) -> int:
    """Bytes already in the model's cache folder (finished blobs + partial .incomplete files)."""
    d = cache_dir(model_id) / "blobs"
    if not d.is_dir():
        return 0
    total = 0
    for f in d.iterdir():
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def expected_bytes(model_id: str, fetcher: Optional[Callable] = None) -> Optional[int]:
    """Size of the files a benchmark downloads, from the Hub's repo record (None = unknown)."""
    rec = fetch_repo(model_id, fetcher=fetcher, cache=_SIZES, token=token())
    files = (rec.get("repo") or {}).get("files") or []
    sizes = [f["size"] for f in files if _wanted(f["name"])]
    return sum(sizes) if sizes and all(s is not None for s in sizes) else None


def free_bytes(model_id: str) -> int:
    p = cache_dir(model_id)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


# ---------------------------------------------------------------------------
# in-process helpers (scripts/vast_models.py at start-up, and the child process)
# ---------------------------------------------------------------------------
def check_access(models: list[str], tok: Optional[str]) -> list[str]:
    """One small download per model; returns the problems (empty = all reachable)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

    problems = []
    for m in models:
        try:
            hf_hub_download(m, "config.json", token=tok)
            print(f"  access ok    {m}")
        except GatedRepoError:
            problems.append(f"{m}: GATED and this token has no access. Open https://huggingface.co/{m}, "
                            "accept the licence with the account that owns HF_TOKEN, wait for approval "
                            "(Meta/Google usually approve within minutes), then re-run.")
        except RepositoryNotFoundError:
            problems.append(f"{m}: not found (or private) for this token" + ("" if tok else
                            " — HF_TOKEN is not set"))
        except HfHubHTTPError as e:
            problems.append(f"{m}: Hugging Face refused the download ({e}); check HF_TOKEN")
        except Exception as e:  # noqa: BLE001 — network etc.
            problems.append(f"{m}: cannot reach Hugging Face ({type(e).__name__}: {e})")
    return problems


def snapshot(model_id: str, tok: Optional[str]) -> None:
    from huggingface_hub import snapshot_download
    snapshot_download(model_id, token=tok, allow_patterns=DOWNLOAD_PATTERNS, ignore_patterns=DOWNLOAD_IGNORE)


# ---------------------------------------------------------------------------
# download on first use (job manager, real GPU)
# ---------------------------------------------------------------------------
def _spawn(model_id: str, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "0"                # only this child goes online
    env["TRANSFORMERS_OFFLINE"] = "0"
    with open(log_path, "wb") as log:          # the child keeps its own handle
        return subprocess.Popen([sys.executable, "-m", "agentmeter.server.model_store", "download", model_id],
                                env=env, stdout=log, stderr=subprocess.STDOUT)


def download_model(model_id: str, report: Callable[..., None], *, total: Optional[int] = None,
                   spawn: Callable[[str, Path], subprocess.Popen] = _spawn,
                   on_disk: Optional[Callable[[str], bool]] = None, poll_s: float = POLL_S) -> None:
    """Download one model in a child process; report bytes as they arrive. Stop-aware."""
    from ..run import runner
    from . import runtime

    if runner._STOP.is_set():
        raise runner.Stopped("stopped by a user before the download started")
    log_path = Path(tempfile.mkstemp(prefix="am_dl_", suffix=".log")[1])
    proc = spawn(model_id, log_path)
    with runner._PROC_LOCK:
        runner._PROC = proc
    try:
        while proc.poll() is None:
            report(done_bytes=cache_bytes(model_id), total_bytes=total)
            time.sleep(poll_s)
        rc = proc.returncode
    finally:
        with runner._PROC_LOCK:
            runner._PROC = None
    tail = ""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        log_path.unlink(missing_ok=True)
    except OSError:
        pass
    if runner._STOP.is_set():
        raise runner.Stopped("stopped by a user during the model download")
    if rc != 0:
        if "GATED" in tail or "GatedRepoError" in tail:
            raise ModelDownloadError(f"{model_id} is gated and the server's HF token has no access: accept its "
                                     f"licence on https://huggingface.co/{model_id} with that account, then retry.",
                                     "gated_no_access")
        last = [ln for ln in tail.strip().splitlines() if ln.strip()][-1:] or ["no output"]
        raise ModelDownloadError(f"downloading {model_id} failed (exit {rc}): {last[0][:300]}", "download_failed")
    if not (on_disk or runtime.model_on_disk)(model_id):
        raise ModelDownloadError(f"{model_id} downloaded but its weights are incomplete on disk", "download_failed")
    report(done_bytes=cache_bytes(model_id), total_bytes=total)


def ensure_job_models(job: dict[str, Any], report: Callable[..., None], *,
                      on_disk: Optional[Callable[[str], bool]] = None,
                      download: Callable[..., None] = download_model,
                      sizes: Callable[[str], Optional[int]] = expected_bytes,
                      free: Callable[[str], int] = free_bytes,
                      have: Callable[[str], int] = cache_bytes) -> list[str]:
    """Download every model of a REAL benchmark that is not on disk yet. Returns those downloaded."""
    from . import runtime

    if job.get("provider") != "hf":
        return []
    disk = on_disk or runtime.model_on_disk
    todo = [m for m in job.get("models") or [] if not disk(m)]
    for i, m in enumerate(todo, 1):
        total = sizes(m)
        need = max(0, (total or 0) - have(m))
        avail = free(m)
        if total is not None and need + DISK_MARGIN_BYTES > avail:
            raise ModelDownloadError(
                f"not enough disk to download {m}: about {need / 1024 ** 3:,.1f} GB needed, "
                f"{avail / 1024 ** 3:,.1f} GB free (keeping {DISK_MARGIN_BYTES / 1024 ** 3:.0f} GB spare). "
                "Rent an instance with a bigger disk.", "low_disk")
        base = {"model": m, "index": i, "count": len(todo)}
        report(**base, phase="downloading", done_bytes=have(m), total_bytes=total)
        download(m, lambda **kw: report(**base, phase="downloading", **kw), total=total)
        report(**base, phase="downloaded", done_bytes=have(m), total_bytes=total)
    return todo


# ---------------------------------------------------------------------------
# 4-bit verification (admin): load exactly as a benchmark does, generate a few tokens
# ---------------------------------------------------------------------------
VERIFY_TIMEOUT_S = 20 * 60
_VERIFY_TAG = "AM_VERIFY "


def _verify_in_process(model_id: str) -> dict[str, Any]:
    """The child's work: HFProvider with the study config (uniform 4-bit NF4, double quant, fp16
    compute), weights memory after load, one short generation, unload."""
    import copy

    from ..config import Config, load_config
    from ..providers.hf import HFProvider
    from . import runtime

    base = load_config(runtime.REAL_BASE_CONFIG)
    data = copy.deepcopy(base.data)
    data["model"]["name"] = model_id
    data["model"]["hf"]["max_new_tokens"] = 8
    t = time.time()
    p = HFProvider(Config(data, base.path))
    p.load()
    weights_mb = None
    try:
        import torch
        if torch.cuda.is_available():
            weights_mb = round(torch.cuda.memory_allocated() / 1024 ** 2, 1)
    except Exception:  # noqa: BLE001
        pass
    out = p.generate("Reply with the single word: ready")
    p.unload()
    text = str(getattr(out, "text", out))
    if not text.strip():
        return {"status": "failed", "error": "the model loaded but generated no text", "weights_mb": weights_mb,
                "seconds": round(time.time() - t, 1)}
    return {"status": "ok", "weights_mb": weights_mb, "seconds": round(time.time() - t, 1), "sample": text[:40]}


def _spawn_verify(model_id: str, log_path: Path) -> subprocess.Popen:
    with open(log_path, "wb") as log:          # offline: verify runs after the download step
        return subprocess.Popen([sys.executable, "-m", "agentmeter.server.model_store", "verify", model_id],
                                stdout=log, stderr=subprocess.STDOUT)


def verify_model(model_id: str, *, spawn: Callable[[str, Path], subprocess.Popen] = _spawn_verify,
                 timeout_s: float = VERIFY_TIMEOUT_S) -> dict[str, Any]:
    """Run the 4-bit test-load in a child process (Stop ends it; the OS frees its GPU memory)."""
    import json as _json

    from ..run import runner

    if runner._STOP.is_set():
        raise runner.Stopped("stopped by a user before verification started")
    log_path = Path(tempfile.mkstemp(prefix="am_verify_", suffix=".log")[1])
    proc = spawn(model_id, log_path)
    with runner._PROC_LOCK:
        runner._PROC = proc
    try:
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = None
    finally:
        with runner._PROC_LOCK:
            runner._PROC = None
    text = ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        log_path.unlink(missing_ok=True)
    except OSError:
        pass
    if runner._STOP.is_set():
        raise runner.Stopped("stopped by a user during verification")
    lines = [ln[len(_VERIFY_TAG):] for ln in text.splitlines() if ln.startswith(_VERIFY_TAG)]
    if lines:
        try:
            return _json.loads(lines[-1])
        except ValueError:
            pass
    if rc is None:
        return {"status": "failed", "error": f"timed out after {timeout_s / 60:.0f} min"}
    last = [ln for ln in text.strip().splitlines() if ln.strip()][-1:] or ["no output"]
    return {"status": "failed", "error": f"exit {rc}: {last[0][:300]}"}


# ---------------------------------------------------------------------------
# the child process
# ---------------------------------------------------------------------------
def _main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="download or 4-bit-verify one model (child process)")
    ap.add_argument("cmd", choices=["download", "verify"])
    ap.add_argument("model")
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        import json as _json
        try:
            res = _verify_in_process(a.model)
        except Exception as e:  # noqa: BLE001 — reported as a failed verification
            res = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:400]}
        print(_VERIFY_TAG + _json.dumps(res), flush=True)
        return 0
    try:
        snapshot(a.model, token())
    except Exception as e:  # noqa: BLE001 — reported to the parent through the exit code + log
        name = type(e).__name__
        print(("GATED: " if "Gated" in name else "") + f"{name}: {e}", flush=True)
        return 3 if "Gated" in name else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
