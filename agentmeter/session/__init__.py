"""Benchmark sessions (service pivot, Phase 38): a prepared user input run -> the
EXISTING instrumented 4-agent pipeline for up to 2 models -> scoring + comparison.

    benchmark.py  validate models, write the session's scenarios + config, run the
                  existing sequential subprocess-per-model runner into
                  <run_dir>/session.db (never the locked study DB)
    scoring.py    reuse analyze.py (phase7 accuracy/confusion, _criteria, phase8
                  SAW via _composite/_tier, sensitivity, per_agent, statistics,
                  provenance) over session.db, plus the <=2-model comparison
"""
