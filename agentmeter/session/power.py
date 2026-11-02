"""GPU board-power sampling during a benchmark session (Phase 46, energy per flow).

The sampler runs in the PARENT process (run_session) while the existing runner's
worker subprocesses do the measured work, so it never touches the measured process:
no CUDA calls, no extra Python work inside the timed agent calls. One NVML power
read is a few microseconds on a separate CPU thread.

  method "nvml"        pynvml.nvmlDeviceGetPowerUsage (mW) + device memory used + GPU utilisation,
                       every `interval_s`
  method "nvidia-smi"  one long-running `nvidia-smi --query-gpu=power.draw,memory.used,utilization.gpu -lms N`
                       process (no process spawn per sample)
  no GPU / mock        nothing is sampled; the session reports energy "not measured"
  "synthetic (test only)"  AGENTMETER_TEST_SYNTHETIC_GPU=1 with the mock provider: invented readings
                       so the end-to-end tests can exercise the GPU charts. Never a measurement; the
                       method name says so wherever it is shown, and energy is never reported from it.

GPU health (alongside, same reads): SM clock, temperature and the driver's clock-throttle reasons
per sample, the maximum SM clock once, and the OTHER processes holding GPU memory while it was
idle for us (foreign_processes) — so a session run on a throttled or shared GPU can be flagged.

Idle baseline: before each model's worker starts (GPU idle, the previous worker has
exited), run_full's before_model hook calls idle_window(), which marks a few seconds of
samples as that model's idle board power (energy.idle_sample_s).
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
        self.mem_samples: list[tuple[float, float]] = []      # (unix_s, device used MB) — NVML view
        self.util_samples: list[tuple[float, float]] = []     # (unix_s, GPU utilisation %)
        self.idle: dict[str, dict[str, float]] = {}            # model -> {"t0", "t1"} idle windows
        self.health_samples: list[tuple[float, Optional[float], Optional[float], Optional[int]]] = []   # (unix_s, SM MHz, °C, throttle bits)
        self.max_sm_mhz: Optional[float] = None
        self.foreign: dict[str, list[dict[str, Any]]] = {}     # model -> processes on the GPU that are not ours
        self._nvml_h = None
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
        self._nvml_h = h
        try:
            self.max_sm_mhz = float(pynvml.nvmlDeviceGetMaxClockInfo(h, pynvml.NVML_CLOCK_SM))
        except Exception:  # noqa: BLE001
            self.max_sm_mhz = None

        def health():
            out = []
            for f in (lambda: float(pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_SM)),
                      lambda: float(pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)),
                      lambda: int(pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(h))):
                try:
                    out.append(f())
                except Exception:  # noqa: BLE001 — health is a bonus
                    out.append(None)
            return out
        self._health = health

        def read():
            w = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(h).used / (1024 * 1024)
            except Exception:  # noqa: BLE001 — memory is a bonus; power is what matters
                mem = None
            try:
                util = float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
            except Exception:  # noqa: BLE001 — utilisation is a bonus too
                util = None
            return w, mem, util
        return read

    def _loop_nvml(self, read) -> None:
        while not self._stop.is_set():
            try:
                t = time.time()
                w, mem, util = read()
                self.samples.append((t, float(w)))
                if mem is not None:
                    self.mem_samples.append((t, float(mem)))
                if util is not None:
                    self.util_samples.append((t, util))
                hs = getattr(self, "_health", None)
                if hs is not None:
                    sm, temp, why = hs()
                    if sm is not None or temp is not None or why is not None:
                        self.health_samples.append((t, sm, temp, why))
            except Exception as e:  # noqa: BLE001 — a failed read is recorded, never raised
                self.error = f"NVML read failed: {e}"
            self._stop.wait(self.interval_s)

    def _loop_smi(self) -> None:
        fields = "power.draw,memory.used,utilization.gpu"
        extra = fields + ",clocks.sm,temperature.gpu,clocks_throttle_reasons.active,clocks.max.sm"
        try:                                             # health fields need a recent driver: probe once
            ok = subprocess.run(["nvidia-smi", f"--id={self.gpu_index}", f"--query-gpu={extra}", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=10).returncode == 0
        except Exception:  # noqa: BLE001
            ok = False
        cmd = ["nvidia-smi", f"--id={self.gpu_index}",
               f"--query-gpu={extra if ok else fields}",
               "--format=csv,noheader,nounits", f"-lms={max(50, int(self.interval_s * 1000))}"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in self._proc.stdout:                    # type: ignore[union-attr]
            if self._stop.is_set():
                break
            parts = [x.strip() for x in line.split(",")]
            t = time.time()
            try:
                self.samples.append((t, float(parts[0])))
            except (ValueError, IndexError):
                continue                                  # "[N/A]" etc.
            try:
                self.mem_samples.append((t, float(parts[1])))
            except (ValueError, IndexError):
                pass
            try:
                self.util_samples.append((t, float(parts[2])))
            except (ValueError, IndexError):
                pass
            def num(i, conv=float):
                try:
                    return conv(parts[i])
                except (ValueError, IndexError):
                    return None
            sm, temp, why = num(3), num(4), num(5, lambda x: int(x, 16))
            if self.max_sm_mhz is None:
                self.max_sm_mhz = num(6)
            if sm is not None or temp is not None or why is not None:
                self.health_samples.append((t, sm, temp, why))

    def _loop_synthetic(self) -> None:
        import math
        t0 = time.time()
        while not self._stop.is_set():
            t = time.time()
            x = t - t0
            self.samples.append((t, 60.0 + 90.0 * (0.5 + 0.5 * math.sin(x * 1.7))))
            self.util_samples.append((t, 50.0 + 45.0 * math.sin(x * 1.7)))
            self.mem_samples.append((t, 4000.0 + 30.0 * x))
            self.health_samples.append((t, 1800.0, 55.0 + 5.0 * math.sin(x), 0))
            self._stop.wait(self.interval_s)

    def start_synthetic(self) -> bool:
        """TEST ONLY (AGENTMETER_TEST_SYNTHETIC_GPU=1): invented readings for the UI tests."""
        self.method = "synthetic (test only)"
        self.max_sm_mhz = 1800.0
        self._thread = threading.Thread(target=self._loop_synthetic, daemon=True, name="agentmeter-power-synthetic")
        self._thread.start()
        return True

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

    def idle_window(self, model: str, seconds: float) -> None:
        """Called (via run_full's before_model hook) while the GPU is idle, right before a
        model's worker starts: mark `seconds` of samples as that model's idle baseline."""
        if self._thread is None or seconds <= 0:
            return
        t0 = time.time()
        self.foreign[model] = self.other_processes()
        time.sleep(seconds)
        self.idle[model] = {"t0": t0, "t1": time.time()}

    def other_processes(self) -> list[dict[str, Any]]:
        """Processes holding GPU memory right now. Called while the GPU is idle for us (between our
        workers), so every one of them is someone else's: a shared GPU. Empty when unknown."""
        out: list[dict[str, Any]] = []
        try:
            if self._nvml_h is not None:
                import pynvml
                for pr in pynvml.nvmlDeviceGetComputeRunningProcesses(self._nvml_h):
                    out.append({"pid": int(pr.pid), "used_mb": round((pr.usedGpuMemory or 0) / 2 ** 20)})
            elif self.method == "nvidia-smi" and shutil.which("nvidia-smi"):
                r = subprocess.run(["nvidia-smi", f"--id={self.gpu_index}", "--query-compute-apps=pid,used_memory",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().splitlines():
                    pid, mb = [x.strip() for x in line.split(",")][:2]
                    out.append({"pid": int(pid), "used_mb": float(mb) if mb.replace(".", "", 1).isdigit() else None})
        except Exception:  # noqa: BLE001 — unknown is not "none"; it is simply not reported
            return []
        return out

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
                "n_samples": len(self.samples), "samples": list(self.samples),
                "mem_samples": list(self.mem_samples), "util_samples": list(self.util_samples),
                "health_samples": list(self.health_samples), "max_sm_mhz": self.max_sm_mhz,
                "foreign_processes": dict(self.foreign),
                "idle": dict(self.idle), "error": self.error}
