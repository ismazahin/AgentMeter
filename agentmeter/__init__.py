"""AgentMeter — benchmarking open-source LLM resource efficiency
in agentic network threat-detection pipelines.

The agentic pipeline is only the TEST SUBJECT. The contribution is the
measurement harness, per-agent instrumentation, and comparison methodology.

Modules are grouped into subpackages by role (see agentmeter/README.md):
  config            configuration loader (used everywhere)
  db/               databases: storage (study results) + appdb (app metadata)
  data/             dataset loading + dataset preparation
  pipeline/         the 4-agent process: pipeline, agents, instrumentation
  providers/        model backends (mock / Hugging Face)
  run/              execution: runner, worker, pilot, measure-vram
  analysis/         accuracy + SAW + per-class + advanced analysis
  server/           web/API layer: pull-eval, CRUD, local sessions, HF metadata, config builder
  util/             env/token helpers, env check, Vast.ai shutdown

Submodules are re-exported here so `from agentmeter import <module>` keeps working.
"""
from . import config, providers
from .db import appdb, combine, storage
from .data import dataprep, dataset
from .pipeline import agents, instrument
from .run import measure, pilot, runner, worker
from .analysis import analyze, analyze_advanced, analyze_by_class, integrity
from .server import config_builder, crud_api, hf_metadata, local_sessions, pull_eval
from .util import env_check, envtools, vast_shutdown

__version__ = "0.0.0"
