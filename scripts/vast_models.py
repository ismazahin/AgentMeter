#!/usr/bin/env python3
"""Phase E — put the 5 study models on this GPU box's disk and prove they load.

    HF_TOKEN=hf_... python scripts/vast_models.py            # check access, download, verify Phi-3
    python scripts/vast_models.py --check                    # only report what is on disk
    python scripts/vast_models.py --verify all               # load every model in 4-bit NF4

Order matters: ACCESS for all five is checked first (one small file each), so a
gated model you have not been granted (meta-llama/Meta-Llama-3-8B-Instruct and
google/gemma-2-9b-it need you to accept their licence on huggingface.co with the
account that owns HF_TOKEN) fails in seconds with the fix, before ~70 GB of
weights start downloading. Weights go to $HF_HOME (scripts/vast_up.sh points it
at the instance's persistent disk). Only safetensors + config/tokenizer files are
fetched (no duplicate original/ or .pth checkpoints).

--verify loads a model exactly as the benchmark does (HFProvider with the study
config: uniform 4-bit NF4, double quant, fp16 compute), generates a few tokens,
and unloads it. Exit code 0 = ready for `serve.py --provider real`.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentmeter.config import load_config  # noqa: E402
from agentmeter.server import model_catalog, model_store, runtime  # noqa: E402

APPROX_GB = {"mistralai/Mistral-7B-Instruct-v0.3": 14.5, "meta-llama/Meta-Llama-3-8B-Instruct": 16.1,
             "Qwen/Qwen2.5-7B-Instruct": 15.3, "microsoft/Phi-3-mini-4k-instruct": 7.7,
             "google/gemma-2-9b-it": 18.5}


def _token() -> str | None:
    return model_store.token()


def check_access(models: list[str], token: str | None) -> list[str]:
    return model_store.check_access(models, token)


def _approx_gb(m: str) -> float:
    if m in APPROX_GB:
        return APPROX_GB[m]
    b = model_store.expected_bytes(m)
    return b / 1024 ** 3 if b else 16.0


def download(models: list[str], token: str | None) -> None:
    for m in models:
        if runtime.model_on_disk(m):
            print(f"  on disk      {m}")
            continue
        t = time.time()
        print(f"  downloading  {m} (~{_approx_gb(m):,.1f} GB) ...", flush=True)
        model_store.snapshot(m, token)
        print(f"  done         {m} in {time.time() - t:,.0f} s", flush=True)


def verify(models: list[str]) -> None:
    import copy

    from agentmeter.config import Config
    from agentmeter.providers.hf import HFProvider

    base = load_config(runtime.REAL_BASE_CONFIG)
    for m in models:
        data = copy.deepcopy(base.data)
        data["model"]["name"] = m
        data["model"]["hf"]["max_new_tokens"] = 8
        cfg = Config(data, base.path)
        t = time.time()
        p = HFProvider(cfg)
        p.load()
        out = p.generate("Reply with the single word: ready")
        p.unload()
        text = getattr(out, "text", out)
        print(f"  4-bit NF4 ok {m}: loaded + generated in {time.time() - t:,.0f} s ({str(text)[:30]!r})",
              flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only report which models are on disk")
    ap.add_argument("--verify", default="microsoft/Phi-3-mini-4k-instruct",
                    help="model to load as a 4-bit smoke test after download: a model id, 'all' or 'none'")
    args = ap.parse_args(argv)

    study = runtime.study_models()
    extra = [m for m in model_catalog.start_up_ids() if m not in study]   # catalog: download_at_start
    models = study + extra
    cache = runtime._hub_cache()
    print(f"Hugging Face cache: {cache}")
    if args.check:
        local = runtime.models_local(models)
        for m, ok in local.items():
            print(f"  {'on disk ' if ok else 'MISSING '} {m}")
        return 0 if all(local.values()) else 1

    token = _token()
    if not token:
        print("WARNING: HF_TOKEN is not set — the gated Llama-3 and gemma-2 downloads will fail.",
              file=sys.stderr)
    print(f"1/3 checking access to {len(models)} models (5 study"
          + (f" + {len(extra)} extra marked download_at_start" if extra else "") + ") before any large download ...")
    problems = check_access(models, token)
    if problems:
        print("\nERROR: cannot get every model:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 3
    need = sum(_approx_gb(m) for m in models if not runtime.model_on_disk(m))
    cache.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cache).free / 1024 ** 3
    if need and free < need + 5:
        print(f"\nERROR: about {need:,.0f} GB still to download but only {free:,.0f} GB free in {cache}. "
              "Rent an instance with a bigger disk (see docs/DEPLOY.md).", file=sys.stderr)
        return 4
    print(f"2/3 downloading (about {need:,.0f} GB to fetch, {free:,.0f} GB free) ...")
    download(models, token)
    missing = [m for m, ok in runtime.models_local(models).items() if not ok]
    if missing:
        print(f"\nERROR: still missing after download: {missing}", file=sys.stderr)
        return 5
    target = [] if args.verify == "none" else (models if args.verify == "all" else [args.verify])
    if target:
        print("3/3 loading in uniform 4-bit NF4 (same settings as the benchmark) ...")
        verify(target)
    print("\nREADY: all 5 study models" + (f" and {len(extra)} extra model(s)" if extra else "") + " are on disk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
