"""Mock model provider (Phases 0-3).

A deterministic stand-in for a real LLM so the pipeline, instrumentation, and
storage can be tested with NO Hugging Face account, token, or GPU.

It is NOT machine learning. It reads the calling agent's role from the system
prompt and applies simple, transparent heuristics over the flow features so the
pipeline produces coherent, class-consistent output end to end. Real model
behaviour arrives with the HF provider in Phase 4.
"""
from __future__ import annotations

import re
import time

from ..config import Config
from .base import GenerationResult, ModelProvider

_FEATURE_RE = re.compile(r"^-\s*(.+?):\s*(.+)$", re.MULTILINE)


def _parse_features(prompt: str) -> dict[str, float]:
    """Pull '- Name: value' lines back into a numeric dict (best-effort)."""
    out: dict[str, float] = {}
    for name, value in _FEATURE_RE.findall(prompt):
        try:
            out[name.strip()] = float(str(value).replace(",", "").strip())
        except ValueError:
            continue
    return out


def _heuristic_class(f: dict[str, float], classes: list[str]) -> str:
    """Transparent rule-of-thumb classifier over network-flow features.

    Deliberately simple; only here so the mock yields plausible verdicts.
    """
    port = f.get("Destination Port", 0)
    dur = f.get("Flow Duration", 0)
    fwd = f.get("Total Fwd Packets", 0)
    bwd = f.get("Total Bwd Packets", 0)
    pps = f.get("Flow Packets/s", 0)
    syn = f.get("SYN Flag Count", 0)
    bwd_bytes = f.get("Total Length of Bwd Packets", 0)

    def pick(name: str, fallback: str = "Benign") -> str:
        return name if name in classes else (classes[0] if classes else fallback)

    # SYN flood: overwhelming SYNs, little/no return traffic.
    if syn >= 1000 and bwd == 0:
        return pick("SYN Flood")
    # Volumetric DDoS: extreme packet rate in both directions.
    if pps >= 100000 and fwd >= 1000 and bwd >= 1000:
        return pick("Volumetric DDoS")
    # Port scanning: tiny, short probe flows carrying a SYN.
    if fwd <= 2 and dur <= 100000 and syn >= 1:
        return pick("Port Scanning")
    # Data exfiltration: large sustained outbound-return payload.
    if bwd_bytes >= 10_000_000 and dur >= 5_000_000:
        return pick("Data Exfiltration")
    # Brute force: repeated auth attempts against SSH/FTP.
    if port in (22, 21) and fwd >= 50:
        return pick("Brute Force")
    return pick("Benign")


class MockProvider(ModelProvider):
    name = "mock"

    def __init__(self, config: Config):
        self.config = config
        self.classes: list[str] = list(config.get("classes", []) or [])
        self.mitre: dict[str, str] = dict(config.get("mitre", {}) or {})
        self.latency_s: float = float(config.get("model.mock.latency_s", 0.0) or 0.0)

    # --- interface ------------------------------------------------------
    def generate(self, prompt: str, system: str | None = None) -> GenerationResult:
        if self.latency_s > 0:
            time.sleep(self.latency_s)

        role = (system or "").lower()
        features = _parse_features(prompt)
        predicted = _heuristic_class(features, self.classes)

        if "perceive" in role:
            text = self._perceive(features)
        elif "reason" in role:
            text = self._reason(features, predicted)
        elif "decide" in role:
            text = f"Classification: {predicted}"
        elif "act" in role:
            technique = self.mitre.get(predicted, "N/A")
            text = (
                f"Predicted class: {predicted}\n"
                f"MITRE ATT&CK: {technique}\n"
                f"Justification: Flow features are most consistent with {predicted}."
            )
        else:
            text = f"Observation: {predicted}"

        return GenerationResult(
            text=text,
            input_tokens=self._count(prompt) + self._count(system or ""),
            output_tokens=self._count(text),
            ttft_s=None,  # mock does not stream; measured for real in Phase 4/5
        )

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _count(text: str) -> int:
        """Rough token estimate (whitespace). Real tokenizer used in Phase 4."""
        return len(text.split())

    @staticmethod
    def _perceive(f: dict[str, float]) -> str:
        port = int(f.get("Destination Port", 0))
        dur = f.get("Flow Duration", 0)
        fwd = int(f.get("Total Fwd Packets", 0))
        bwd = int(f.get("Total Bwd Packets", 0))
        pps = f.get("Flow Packets/s", 0)
        return (
            f"Summary: flow to port {port}, duration {dur:.0f} us, "
            f"{fwd} fwd / {bwd} bwd packets, ~{pps:.0f} packets/s."
        )

    @staticmethod
    def _reason(f: dict[str, float], predicted: str) -> str:
        syn = int(f.get("SYN Flag Count", 0))
        pps = f.get("Flow Packets/s", 0)
        return (
            f"The flow shows SYN count {syn} and rate ~{pps:.0f} pkt/s, "
            f"behaviour consistent with {predicted}."
        )
