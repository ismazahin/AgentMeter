"""Deploy the cloud data-preparation service to a free Hugging Face Space (Docker; no card).

The same service as Google Cloud Run (cloudprep/app.py with the browser's preparation bundle,
so prepared flows and their hash are identical), on Hugging Face's free CPU Space (2 vCPU,
16 GB). A free Space sleeps after 48 hours without use; AgentMeter wakes it when New benchmark
opens, and a job that still finds it asleep is prepared in the browser instead.

    pip install huggingface_hub
    python scripts/deploy_cloudprep_hf.py                 # asks for a Hugging Face WRITE token

Options: --space NAME (agentmeter-prep), --no-supabase (only print the Supabase command).
Environment: HF_TOKEN (the write token), CLOUD_PREP_SECRET (reuse a secret; otherwise a new one).
When it is done it runs `npx supabase secrets set CLOUD_PREP_URL=... CLOUD_PREP_SECRET=...`, so
the secret never has to be copied by hand.
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = """---
title: AgentMeter cloud data preparation
emoji: ⚙️
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8080
pinned: false
short_description: Prepares AgentMeter datasets while the GPU benchmarks
---

The AgentMeter cloud data-preparation service (cloudprep/app.py in the AgentMeter repository).
AgentMeter measures LLM resource efficiency; it is not a threat-detection product. Only jobs
signed by the AgentMeter control plane are accepted; `GET /health` reports that it is up.
"""


def stage(dest: Path) -> Path:
    """The Space's files: the Cloud Run build folder plus the Space's README (sdk: docker)."""
    spec = importlib.util.spec_from_file_location("build_web_prep", REPO / "scripts" / "build_web_prep.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.main()                                               # the bundle must match the code deployed
    (dest / "wheels").mkdir(parents=True, exist_ok=True)
    for f in ("cloudprep/Dockerfile", "cloudprep/app.py", "cloudprep/requirements.txt",
              "web/prep/agentmeter-prep.zip", "agentmeter/server/urlfetch.py"):
        shutil.copy2(REPO / f, dest / Path(f).name)
    for w in (REPO / "web" / "prep" / "wheels").glob("*.whl"):
        shutil.copy2(w, dest / "wheels" / w.name)
    (dest / "README.md").write_text(README, encoding="utf-8")
    return dest


def space_url(repo_id: str) -> str:
    return "https://" + repo_id.replace("/", "-").replace("_", "-").replace(".", "-").lower() + ".hf.space"


def wait_running(api, repo_id: str, minutes: int = 20) -> str:
    end = time.time() + minutes * 60
    last = None
    while time.time() < end:
        stage_ = api.get_space_runtime(repo_id).stage
        if stage_ != last:
            print(f"  Space: {stage_}")
            last = stage_
        if stage_ == "RUNNING":
            return stage_
        if stage_ in ("BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"):
            raise SystemExit(f"the Space failed ({stage_}); see its logs on huggingface.co/spaces/{repo_id}")
        time.sleep(10)
    raise SystemExit("the Space is still building; run this script again later, or check its page")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--space", default="agentmeter-prep", help="Space name (default: agentmeter-prep)")
    ap.add_argument("--no-supabase", action="store_true", help="print the Supabase command instead of running it")
    a = ap.parse_args()
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Install the Hugging Face client first:  pip install huggingface_hub", file=sys.stderr)
        return 2
    token = os.environ.get("HF_TOKEN") or getpass.getpass("Hugging Face WRITE token (huggingface.co > Settings > Access Tokens): ").strip()
    api = HfApi(token=token)
    user = api.whoami()["name"]
    repo_id = f"{user}/{a.space}"
    secret = os.environ.get("CLOUD_PREP_SECRET") or secrets.token_urlsafe(48)

    print(f"Creating or updating the Space {repo_id} ...")
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True, private=False)
    api.add_space_secret(repo_id, "CLOUD_PREP_SECRET", secret)     # a Space secret: never in the files
    with tempfile.TemporaryDirectory() as tmp:
        api.upload_folder(folder_path=str(stage(Path(tmp))), repo_id=repo_id, repo_type="space",
                          commit_message="Deploy the AgentMeter cloud data-preparation service")
    print("Uploaded. Building (the first build takes a few minutes) ...")
    try:
        api.restart_space(repo_id)            # picks up the secret even when the files did not change
    except Exception:  # noqa: BLE001 — a fresh build restarts it anyway
        pass
    wait_running(api, repo_id)
    url = space_url(repo_id)
    for _ in range(30):
        try:
            with urllib.request.urlopen(url + "/health", timeout=20) as r:
                print("  health:", r.read().decode()[:200])
                break
        except OSError:
            time.sleep(5)
    print(f"\nDeployed: {url}")

    cmd = ["npx", "supabase", "secrets", "set", f"CLOUD_PREP_URL={url}", f"CLOUD_PREP_SECRET={secret}"]
    if a.no_supabase:
        print("Now run (it holds the secret: do not share it):\n  " + " ".join(cmd))
        return 0
    print("Telling the control plane where it is (npx supabase secrets set ...) ...")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("npx not found. Run this yourself (it holds the secret: do not share it):\n  " + " ".join(cmd))
        return 1
    r = subprocess.run([npx] + cmd[1:], cwd=REPO)
    if r.returncode != 0:
        print("That failed. Run it yourself from the repo folder:\n  " + " ".join(cmd))
        return 1
    print("Done. Then:  npx supabase db push   and   npx supabase functions deploy agentmeter --no-verify-jwt --use-api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
