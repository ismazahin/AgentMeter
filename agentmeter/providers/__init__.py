"""Model provider adapters.

One interface (`ModelProvider.generate`) with swappable implementations, so a
different model is a config change, not a code change (build plan sec 4.4).

  - mock : Phases 0-3. No HF account, token, or GPU.
  - hf   : Phase 4+. In-process on the GPU (Mode A), VRAM measurable.
"""
from __future__ import annotations

from ..config import Config
from .base import GenerationResult, ModelProvider
from .mock import MockProvider

__all__ = ["GenerationResult", "ModelProvider", "MockProvider", "get_provider"]


def get_provider(config: Config) -> ModelProvider:
    """Build the provider named by config.model.provider."""
    provider = config.get("model.provider")
    if provider == "mock":
        return MockProvider(config)
    if provider == "hf":
        # Introduced in Phase 4. Import lazily so Phases 0-3 need no GPU deps.
        try:
            from .hf import HFProvider
        except ImportError as exc:  # pragma: no cover - Phase 4 not built yet
            raise NotImplementedError(
                "model.provider 'hf' (in-process GPU, Mode A) is added in Phase 4. "
                f"Import failed: {exc}"
            ) from exc
        return HFProvider(config)
    raise ValueError(f"Unknown model.provider: {provider!r} (expected 'mock' or 'hf')")
