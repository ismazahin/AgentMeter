"""The IDS gate: a flow-based Random Forest detector placed before the LLM triage pipeline.

The IDS DETECTS (attack or not, a score 0-1); only flows it flags (score >= threshold) reach the
LLM agents, which TRIAGE them. Flows it clears are dismissed without an LLM call. The model is
trained offline (scripts/train_ids.py) on a hold-out-separated split of a CIC-IDS2017-format
dataset; see agentmeter/ids/model.py.
"""
from .model import (IdsError, IdsModel, default_model_path, feature_hashes, load_model,  # noqa: F401
                    model_available)
