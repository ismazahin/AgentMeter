"""AgentMeter backend server: the benchmark service API + the read-only baseline page.

AgentMeter MEASURES LLM resource efficiency (and accuracy where labels exist); it
is not a threat-detection product. One Flask process serves:

    /service, /config.json            the Prepare -> Benchmark front-end (web/; the same
                                      static files can be hosted on Vercel / Pages)
    /api/prepare, /api/prepared/...   Prepare: upload or URL import -> prepared set
    /api/jobs, /api/jobs/<id>/...     Benchmark jobs, results JSON, PDF report
    /api/service/config, /health      provider, limits, GPU, queue
    /                                 "Validation baseline": the locked 5-model study,
                                      read-only (Overview + Detailed analysis)
    /api/model-metadata               Hugging Face Hub metadata for the 5 study models
                                      (external context shown on the baseline page)
    /api/notify-status|-test|-check   Telegram notifications (server-side .env)

Typical starts:

    python scripts/serve.py                         # local dev: auto provider (mock w/o GPU)
    python scripts/serve.py --provider real --host 127.0.0.1   # GPU box behind a tunnel
    bash scripts/vast_up.sh                         # the whole Vast.ai bring-up (docs/DEPLOY.md)

Access control, CORS and rate limits: agentmeter/server/access.py. Provider and the
real-mode preflight: agentmeter/server/runtime.py.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

# Allow "python scripts/serve.py" from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentmeter import envtools, vast_shutdown  # noqa: E402

log = logging.getLogger("agentmeter.serve")

DASHBOARD_DIR = REPO_ROOT / "dashboard"     # the read-only baseline page
WEB_DIR = REPO_ROOT / "web"                 # the static service front-end
# Only these baseline-page assets are servable (no arbitrary file access).
# analysis.json: a canonical results file dropped in dashboard/ auto-loads (gitignored).
_ALLOWED_ASSETS = {"index.html", "saw.js", "report.js", "sample_analysis.json", "README.md",
                   "analysis.json"}
# Requests that must not count as "activity" for the idle cost guard: a front-end
# polling /health would otherwise keep a rented GPU alive forever.
_PASSIVE_PATHS = {"/health", "/config.json"}


class CostGuard:
    """Cost safety on a rented GPU box: destroy the Vast.ai instance after
    `idle_timeout` with no request and no queued or running service job. Fires at
    most once and never while a job is queued or running. Off unless auto_destroy."""

    def __init__(self, auto_destroy: bool, idle_timeout_min: float, instance_id=None,
                 destroy=vast_shutdown.destroy_instance, job_manager=None):
        self.auto_destroy = bool(auto_destroy)
        self.idle_timeout = float(idle_timeout_min) * 60.0   # seconds; 0 disables
        self.instance_id = instance_id
        self.job_manager = job_manager
        self._destroy = destroy
        self.last_activity = time.time()
        self._lock = threading.Lock()
        self._destroyed = False

    def touch(self) -> None:
        self.last_activity = time.time()

    def _fire(self, message: str) -> bool:
        with self._lock:
            if self._destroyed:
                return False
            self._destroyed = True
        log.warning(message)
        return self._destroy(instance_id=self.instance_id)

    def busy(self) -> bool:
        jm = self.job_manager
        return bool(jm is not None and any(j["status"] in ("queued", "running")
                                           for j in jm.list_jobs(limit=200)))

    def watchdog(self, stop: threading.Event, poll: float = None) -> None:
        if not self.auto_destroy or self.idle_timeout <= 0:
            return
        interval = poll if poll else min(30.0, max(5.0, self.idle_timeout / 4.0))
        while not stop.wait(interval):
            if self.busy():                  # a long benchmark is activity, not idleness
                self.touch()
                continue
            if time.time() - self.last_activity >= self.idle_timeout:
                self._fire(f"idle for >= {self.idle_timeout/60:.0f} min with no queued or running "
                           "job; destroying instance")
                return


def create_app(dashboard_dir: Path = DASHBOARD_DIR, guard: "CostGuard" = None, app_store=None,
               app_db_path: Path = None, local_results_dir: Path = None,
               start_watcher: bool = False, job_manager=None, mode: str = None):
    from flask import Flask, jsonify, request, send_from_directory

    from agentmeter import appdb, hf_metadata
    from agentmeter.server import access, jobs as _jobs, jobs_api, notify, runtime, service_api

    app = Flask(__name__, static_folder=None)
    results_root = Path(local_results_dir or (REPO_ROOT / "results"))

    # Telegram: notify once per NEW analysis.json under results/ (e.g. a baseline
    # re-run finishing). Seeded with what exists now. Phase D re-wires notify to
    # service job completion; the module and these routes stay for that.
    results_watcher = notify.NewResultsWatcher(results_root)
    results_watcher.seed()

    # App metadata DB — only the Hugging Face metadata cache lives there now. Opened
    # LAZILY; separate from the locked study DB.
    _store_holder = {"store": app_store}

    def get_store():
        if _store_holder["store"] is None:
            _store_holder["store"] = appdb.AppStore(app_db_path or appdb.DEFAULT_APP_DB)
        return _store_holder["store"]

    @app.after_request
    def _bump_activity(resp):  # noqa: ANN001
        if guard is not None and request.path not in _PASSIVE_PATHS and request.method != "OPTIONS":
            guard.touch()
        return resp

    # Provider decided ONCE here (real | mock; no fallback later).
    if mode is None:
        mode = runtime.resolve_mode(None, gpu_available=job_manager.gpu_available() if job_manager else None)
    app.config["SERVICE_MODE"] = mode

    # Benchmark jobs (/api/jobs); the manager is created lazily and recovers its store.
    _jobs_holder = {"mgr": job_manager}

    def get_job_manager():
        if _jobs_holder["mgr"] is None:
            _jobs_holder["mgr"] = _jobs.JobManager()
        return _jobs_holder["mgr"]

    def _queue_depth():
        return sum(1 for j in get_job_manager().list_jobs(limit=200) if j["status"] in ("queued", "running"))

    access.install(app, mode=mode, queue_depth=_queue_depth)    # CORS + passcode + rate limits
    jobs_api.register_jobs(app, get_job_manager, provider=runtime.job_provider(mode),
                           base_config=(str(runtime.REAL_BASE_CONFIG) if mode == "real" else None),
                           environment=lambda: runtime.environment(mode))
    service_api.register_service(app, get_job_manager)

    # --- the service front-end (same files as web/ on a static host) -------------------
    @app.route("/service", methods=["GET"])
    def service_page():
        return send_from_directory(WEB_DIR, "index.html")

    @app.route("/config.json", methods=["GET"])
    def web_config():
        return jsonify({"api_base": ""})       # served by the backend: talk to this origin

    @app.route("/service/prepare", methods=["GET"])
    @app.route("/service/benchmark", methods=["GET"])
    def service_step_page():
        from flask import redirect
        return redirect("/service#/" + request.path.rsplit("/", 1)[-1], code=302)

    # --- the read-only baseline page ----------------------------------------------
    @app.route("/", methods=["GET"])
    def index():
        return send_from_directory(dashboard_dir, "index.html")

    @app.route("/<path:asset>", methods=["GET"])
    def dashboard_asset(asset):
        if asset not in _ALLOWED_ASSETS:
            return jsonify({"error": "not found"}), 404
        return send_from_directory(dashboard_dir, asset)

    # Hugging Face Hub metadata (external CONTEXT: parameters, licence, downloads,
    # last-modified; cached in the app DB; never a study number, never a re-rank).
    def _hf_cache():
        try:
            return hf_metadata.make_store_cache(get_store())
        except Exception:  # noqa: BLE001 — the cache is optional; never break the page
            return None

    @app.route("/api/model-metadata", methods=["GET"])
    def model_metadata_ep():
        model = (request.args.get("model") or "").strip()
        token = hf_metadata.token_from_env()
        cache = _hf_cache()
        if model:
            return jsonify(hf_metadata.get_metadata(model, cache=cache, token=token))
        return jsonify({"models": hf_metadata.get_many(hf_metadata.CANONICAL_MODELS,
                                                       cache=cache, token=token)})

    # --- Telegram (status shows presence only, never a token value) ---------------
    @app.route("/api/notify-status", methods=["GET"])
    def notify_status_ep():
        return jsonify(notify.notify_status())

    @app.route("/api/notify-test", methods=["POST"])
    def notify_test_ep():
        return jsonify(notify.send_telegram(
            "AgentMeter: test notification — your Telegram alerts are working."))

    @app.route("/api/notify-check", methods=["POST"])
    def notify_check_ep():
        return jsonify({"new": results_watcher.poll_once()})

    # --- health -------------------------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health():
        """Open, cheap: what the front-end shows before anyone clicks Run."""
        jm = get_job_manager()
        jobs = jm.list_jobs(limit=200)
        env = runtime.environment(mode)
        acc = app.config.get("ACCESS") or {}
        return jsonify({
            "ok": True, "service": "AgentMeter benchmark backend — measures LLM resource efficiency "
                                   "(not a threat-detection product)",
            "provider": mode,
            "gpu": ({"name": env["gpu_name"], "vram_total_mb": env["gpu_vram_total_mb"],
                     "driver_version": env["driver_version"], "cuda": env["cuda_runtime_version"]}
                    if mode == "real" else None),
            "models_local": runtime.models_local() if mode == "real" else {},
            "queue": {"running": jm.running_job() is not None,
                      "queued": sum(1 for j in jobs if j["status"] == "queued")},
            "auth_required": bool(acc.get("auth_required")),
        })

    if start_watcher:
        def _watch_loop():
            while True:
                try:
                    results_watcher.poll_once()
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("agentmeter.notify").warning("watch poll failed: %s", e)
                time.sleep(15)
        threading.Thread(target=_watch_loop, daemon=True).start()

    return app


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentMeter backend: benchmark service + baseline page.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address; 127.0.0.1 when only a Cloudflare Tunnel on the same "
                         "box should reach it (scripts/vast_up.sh does this)")
    ap.add_argument("--provider", choices=("real", "mock", "auto"), default=None,
                    help="real = HF models on this GPU (refuses to start without a GPU, the "
                         "models on disk and AGENTMETER_PASSCODE); mock = demo; auto (default, or "
                         "AGENTMETER_SERVICE_PROVIDER) = real iff CUDA is visible")
    ap.add_argument("--auto-destroy", action="store_true",
                    help="DESTROY this Vast.ai instance after --idle-timeout minutes with no request "
                         "and no queued/running job (cost safety). DEFAULT OFF.")
    ap.add_argument("--idle-timeout", type=float, default=30.0,
                    help="idle minutes before self-destroy when --auto-destroy is set; 0 disables")
    ap.add_argument("--instance-id", default=None,
                    help="Vast.ai instance id to destroy (else VAST_INSTANCE_ID / Vast env vars)")
    ap.add_argument("--app-db", default=None,
                    help="app metadata DB (Hugging Face metadata cache); default "
                         "results/agentmeter_app.db (separate from the locked study DB)")
    ap.add_argument("--results-dir", default=None,
                    help="results/ directory watched for new analyses (Telegram); default results/")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    envtools.load_env()            # .env tokens; existing environment values win; never logged
    envtools.log_token_status()

    # Decide the provider ONCE; real mode must pass its preflight or the server does
    # not start (never a silent fallback to mock).
    from agentmeter.server import runtime
    try:
        mode = runtime.resolve_mode(args.provider)
        if mode == "real":
            env = runtime.preflight()
            print(f"  provider     : REAL — {env['gpu_name']} ({env['gpu_vram_total_mb']} MB), driver "
                  f"{env['driver_version']}, CUDA {env['cuda_runtime_version']}", flush=True)
        else:
            print("  provider     : MOCK (demo) — no language model runs on this server", flush=True)
    except runtime.RealModeError as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        return 2

    from agentmeter.server.jobs import JobManager
    job_manager = JobManager()      # built now so it recovers interrupted / queued jobs at startup
    instance_id = args.instance_id or vast_shutdown.get_instance_id()
    guard = CostGuard(auto_destroy=args.auto_destroy, idle_timeout_min=args.idle_timeout,
                      instance_id=instance_id, job_manager=job_manager)
    app = create_app(guard=guard, app_db_path=args.app_db, local_results_dir=args.results_dir,
                     start_watcher=True, job_manager=job_manager, mode=mode)

    print("=" * 72)
    print(f"  AgentMeter backend : http://{args.host}:{args.port}/service   (local IP {_local_ip()})")
    print("  Baseline page      : /           Health: /health")
    if not args.auto_destroy:
        print("  Cost safety        : auto-destroy OFF — destroy the instance yourself when done.")
    else:
        have_key = bool(vast_shutdown.get_api_key())
        print(f"  Cost safety        : auto-destroy after {args.idle_timeout:.0f} idle min | instance "
              f"{instance_id or 'UNKNOWN'} | VAST_API_KEY {'present' if have_key else 'MISSING'}")
        if not have_key or not instance_id:
            print("  WARNING            : no API key / instance id — self-destroy will NO-OP.")
    print("=" * 72, flush=True)

    if args.auto_destroy and args.idle_timeout > 0:
        threading.Thread(target=guard.watchdog, args=(threading.Event(),), daemon=True).start()

    # ONE process (threaded, no extra workers): the job layer's single-job lock is per process.
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
