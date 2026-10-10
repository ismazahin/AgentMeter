"""GPU board-power sampling during a benchmark session (Phase 46, energy per flow).

The sampler runs in the PARENT process (run_session) while the existing runner's
worker subprocesses do the measured work, so it never touches the measured process:
no CUDA calls, no extra Python work inside the timed agent calls. One NVML power
read is a few microseconds on a separate CPU thread.

  method "nvml"        pynvml.nvmlDeviceGetPowerUsage (mW) every `interval_s`
  method "nvidia-smi"  one long-running `nvidia-smi --query-gpu=power.draw -lms N`
                       process (no process spawn per sample)
  no GPU / mock        nothing is sampled; the session reports energy "not measured"
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Any, Optional

DEFAULT_INTERVAL_S = 0.5


class PowerSampler:
    def __init__(self, interval_s: float = DEFAULT_INTERVAL_S, gpu_index: int = 0):
        self.interval_s = float(interval_s)
        self.gpu_index = int(gpu_index)
        self.samples: list[tuple[float, float]] = []
        self.method: Optional[str] = None
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

    # --- backends -----------------------------------------------------------------
    def _nvml_reader(self):
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(self.gpu_index)
        pynvml.nvmlDeviceGetPowerUsage(h)                 # probe once: raises if unsupported
        return lambda: pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0

    def _loop_nvml(self, read) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append((time.time(), float(read())))
            except Exception as e:  # noqa: BLE001 — a failed read is recorded, never raised
                self.error = f"NVML read failed: {e}"
            self._stop.wait(self.interval_s)

    def _loop_smi(self) -> None:
        cmd = ["nvidia-smi", f"--id={self.gpu_index}", "--query-gpu=power.draw",
               "--format=csv,noheader,nounits", f"-lms={max(50, int(self.interval_s * 1000))}"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in self._proc.stdout:                    # type: ignore[union-attr]
            if self._stop.is_set():
                break
            try:
                self.samples.append((time.time(), float(line.strip())))
            except ValueError:
                continue                                  # "[N/A]" etc.

    # --- public ---------------------------------------------------------------------
    def start(self) -> bool:
        """Start sampling; False (with .error) when no GPU power source is available."""
        try:
            read = self._nvml_reader()
            self.method = "nvml"
            self._thread = threading.Thread(target=self._loop_nvml, args=(read,), daemon=True,
                                            name="agentmeter-power")
        except Exception as e:  # noqa: BLE001
            if shutil.which("nvidia-smi"):
                self.method = "nvidia-smi"
                self._thread = threading.Thread(target=self._loop_smi, daemon=True, name="agentmeter-power")
            else:
                self.error = f"no GPU power source (NVML: {e}; nvidia-smi not found)"
                return False
        self._thread.start()
        return True

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        return self.result()

    def result(self) -> dict[str, Any]:
        return {"method": self.method, "interval_s": self.interval_s, "gpu_index": self.gpu_index,
                "n_samples": len(self.samples), "samples": list(self.samples), "error": self.error}
