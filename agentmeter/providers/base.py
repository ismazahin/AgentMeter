"""Provider interface shared by the mock and HF adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class GenerationResult:
    """Everything the instrumentation layer needs from one generation call."""

    text: str
    input_tokens: int
    output_tokens: int
    ttft_s: float | None = None   # time-to-first-token; None if not measured (mock)


class ModelProvider(ABC):
    """A single model, used by all agents in a run (Cara 1: same model per run)."""

    name: str = "base"

    def load(self) -> None:
        """Load weights onto the device. No-op for providers that need none."""

    def unload(self) -> None:
        """Free the device / VRAM. No-op unless overridden."""

    @abstractmethod
    def generate(
        self, prompt: str, system: str | None = None, max_new_tokens: int | None = None
    ) -> GenerationResult:
        """Run one inference call and return text + token counts + TTFT.

        max_new_tokens, when given, overrides the provider default for this call
        (used for per-agent token budgets). None means use the default.
        """
        raise NotImplementedError
