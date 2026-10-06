"""Phase 11 — admin model-pull + on-demand evaluation SERVER (Vast.ai GPU host).

A tiny Flask app that BOTH serves the dashboard AND exposes the pull/eval API from
ONE origin, so the dashboard and the API are same-origin (no CORS needed). It lets
the dashboard pull ONE extra HF model and run it through the EXISTING harness for a
side-by-side comparison, without touching the locked 5-model study. ALL the heavy
lifting and every integrity rule live in agentmeter/pull_eval.py — this file is
just the HTTP surface (dashboard + JSON API).

Target host: a Vast.ai GPU instance (a real Linux VM with open, mapped ports) —
NOT Colab. Bind 0.0.0.0 so Vast.ai's port mapping can reach it, then open the URL
printed on startup. Run AFTER the locked study analysis.json exists:

    pip install -r requirements.txt -r requirements-gpu.txt   # flask + flask-cors
    export HF_TOKEN=...                                        # for gated models
    python scripts/pull_eval_server.py                         # binds 0.0.0.0:8000

Open the printed URL (http://<instance-ip>:<mapped-port>/). The dashboard is served
at / and calls /pull-eval, /status, /analysis as SAME-ORIGIN relative paths — no
ngrok and no CORS config required.

Fallbacks:
  * flask-cors enables permissive CORS on the API routes, so opening
    dashboard/index.html directly over file:// (origin "null") and pointing its
    base-URL field at this server also works.
  * --ngrok opens a public tunnel (optional; only if the instance's port is not
    directly reachable). The URL is printed, never hard-coded.

Endpoints:
    GET  /                                  -> dashboard (index.html)
    GET  /saw.js /pull-config.js /...       -> dashboard static assets
    POST /pull-eval   {"model_id": "..."}  -> start a run (validates size FIRST)
    GET  /status                            -> {state, model_id, progress, scenario, error}
    GET  /analysis                          -> merged canonical + exploratory analysis
    GET  /health                            -> liveness
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

# Allow "python scripts/pull_eval_server.py" from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentmeter import envtools, pull_eval, vast_shutdown  # noqa: E402

log = logging.getLogger("agentmeter.pull_eval_server")


class CostGuard:
    """Cost-safety coordinator: destroys the Vast.ai instance once a run finishes
    (results already flushed to disk) or after an idle timeout. Destroy fires at
    most once, and NEVER while a job is running.

    All destroy paths are gated on `auto_destroy`; with it off nothing self-destroys.
    """

    def __init__(self, manager, auto_destroy: bool, idle_timeout_min: float,
                 instance_id=None, destroy=vast_shutdown.destroy_instance):
        self.manager = manager
        self.auto_destroy = bool(auto_destroy)
        self.idle_timeout = float(idle_timeout_min) * 60.0   # seconds; 0 disables
        self.instance_id = instance_id
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

    def on_complete(self, state) -> None:
        """Fired by JobManager AFTER a run's merged analysis is flushed to disk.
        This is the very last step: results are already safely persisted."""
        self.touch()
        if self.auto_destroy:
            self._fire("results persisted; destroying instance")

    def watchdog(self, stop: threading.Event, poll: float = None) -> None:
        """Destroy the instance after `idle_timeout` of no activity and no running
        job. No-op unless auto-destroy is on and a positive timeout is set."""
        if not self.auto_destroy or self.idle_timeout <= 0:
            return
        interval = poll if poll else min(30.0, max(5.0, self.idle_timeout / 4.0))
        while not stop.wait(interval):
            # NEVER destroy while a job is running (a 300-scenario run is long);
            # treat an active job as activity so the idle clock restarts after it.
            if self.manager.snapshot().is_active():
                self.touch()
                continue
            if time.time() - self.last_activity >= self.idle_timeout:
                self._fire(
                    f"idle for >= {self.idle_timeout/60:.0f} min with no running "
                    "job; destroying instance")
                return

DASHBOARD_DIR = REPO_ROOT / "dashboard"
# Only these dashboard assets are servable (no arbitrary file access). analysis.json
# is included so a canonical results file dropped in dashboard/ auto-loads in the
# served dashboard (the "view real results" path); it is gitignored, never a secret.
_ALLOWED_ASSETS = {
    "index.html", "saw.js", "report.js", "pull-config.js", "sample_analysis.json",
    "README.md", "analysis.json", "service.html",
}


def _manual_cors(resp):
    """Fallback permissive CORS (used only if flask-cors is not installed), so a
    file:// dashboard (origin "null") can still reach the API."""
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return resp


def create_app(manager: "pull_eval.JobManager", dashboard_dir: Path = DASHBOARD_DIR,
               guard: "CostGuard" = None, app_store=None,
               app_db_path: Path = None, local_results_dir: Path = None,
               start_watcher: bool = False, job_manager=None):
    import json as _json

    from flask import Flask, Response, jsonify, request, send_from_directory

    from agentmeter import appdb, config_builder, crud_api, hf_metadata, local_sessions
    from agentmeter.server import notify

    app = Flask(__name__, static_folder=None)

    # Phase 16 — read-only discovery of result JSON files under results/.
    local = local_sessions.LocalSessions(local_results_dir or (REPO_ROOT / "results"))

    # Phase 30 — watch results/ for NEW analyses and push a Telegram notification.
    # Seeded with what exists now, so only analyses that appear later notify.
    results_watcher = notify.NewResultsWatcher(local.root)
    results_watcher.seed()

    # Metadata DB (Phase 12 CRUD) — opened LAZILY on first CRUD use, so serving the
    # dashboard / pull API never creates it. Separate from the locked study DB.
    _store_holder = {"store": app_store}

    def get_store():
        if _store_holder["store"] is None:
            _store_holder["store"] = appdb.AppStore(app_db_path or appdb.DEFAULT_APP_DB)
        return _store_holder["store"]

    # Every request counts as activity, so the idle watchdog only fires on a truly
    # quiet server (registered first so it always runs).
    @app.after_request
    def _bump_activity(resp):  # noqa: ANN001
        if guard is not None:
            guard.touch()
        return resp

    # Prefer flask-cors; fall back to manual headers if it is not installed. The
    # dashboard is served SAME-ORIGIN from this app, so CORS is only a fallback
    # for the file:// case (permissive across all routes, including CRUD).
    try:
        from flask_cors import CORS

        CORS(app)
    except Exception:  # noqa: BLE001 — flask-cors optional
        @app.after_request
        def after(resp):  # noqa: ANN001
            return _manual_cors(resp)

    # Phase 31 — optional HTTP Basic Auth gate (for internet exposure). OFF by
    # default (local use unchanged); ENABLED when AGENTMETER_AUTH_PASS is set in the
    # environment/.env. When on, EVERY route needs the user/pass (browsers prompt
    # once and reuse it for fetches too), except CORS preflight and /health. Use
    # only over HTTPS (a tunnel/reverse proxy provides TLS) — Basic Auth is plaintext.
    import hmac as _hmac
    auth_user = os.environ.get("AGENTMETER_AUTH_USER", "agentmeter")
    auth_pass = os.environ.get("AGENTMETER_AUTH_PASS", "")
    app.config["AUTH_ENABLED"] = bool(auth_pass)
    if auth_pass:
        @app.before_request
        def _require_auth():  # noqa: ANN202
            if request.method == "OPTIONS" or request.path == "/health":
                return None
            a = request.authorization
            ok = (a is not None and (getattr(a, "type", "basic") or "basic") == "basic"
                  and _hmac.compare_digest((a.username or ""), auth_user)
                  and _hmac.compare_digest((a.password or ""), auth_pass))
            if not ok:
                return Response("Authentication required", 401,
                                {"WWW-Authenticate": 'Basic realm="AgentMeter"'})
            return None

    # Phase 12 — CRUD routes for sessions / presets / notes.
    crud_api.register_crud(app, get_store)

    # Phase 40 — persistent background benchmark JOBS (/api/jobs). The manager is
    # created LAZILY on first use, so serving the dashboard never touches
    # results/jobs; on creation it recovers jobs left running by a dead process.
    from agentmeter.server import jobs as _jobs, jobs_api
    _jobs_holder = {"mgr": job_manager}

    def get_job_manager():
        if _jobs_holder["mgr"] is None:
            _jobs_holder["mgr"] = _jobs.JobManager()
        return _jobs_holder["mgr"]

    jobs_api.register_jobs(app, get_job_manager)

    # Phase 41 — the service web flow: upload/ingest, run summaries, service config,
    # and the /service page (same origin; the analysis dashboard stays at /).
    from agentmeter.server import service_api
    service_api.register_service(app, get_job_manager)

    @app.route("/service", methods=["GET"])
    def service_page():
        return send_from_directory(dashboard_dir, "service.html")

    # Phase 16 — read-only discovery of result JSON files under results/.
    @app.route("/api/local-sessions", methods=["GET"])
    def local_sessions_list():
        return jsonify({"files": local.list()})

    @app.route("/api/local-sessions/<path:name>", methods=["GET"])
    def local_sessions_get(name):
        try:
            return jsonify(local.read_json(name))
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except _json.JSONDecodeError:
            return jsonify({"error": "file is not valid JSON"}), 400

    # Phase 22 — Hugging Face Hub model metadata (external CONTEXT only; cached in
    # the SEPARATE app DB; degrades gracefully; never a study number).
    def _hf_cache():
        try:
            return hf_metadata.make_store_cache(get_store())
        except Exception:  # noqa: BLE001 — cache is optional; never break the panel
            return None

    @app.route("/api/model-metadata", methods=["GET"])
    def model_metadata_ep():
        model = (request.args.get("model") or "").strip()
        token = hf_metadata.token_from_env()
        cache = _hf_cache()
        if model:
            return jsonify(hf_metadata.get_metadata(model, cache=cache, token=token))
        # batch: the 5 canonical study models
        return jsonify({"models": hf_metadata.get_many(
            hf_metadata.CANONICAL_MODELS, cache=cache, token=token)})

    # Phase 18 — config builder. Writes a VALID run config to configs/user/ from
    # UI inputs; NEVER touches the locked study config or root config, and NEVER
    # starts a run (config generation only, no GPU work here).
    def _rel(path):
        """Repo-relative path when possible, else the absolute path (used only for
        display in the JSON response)."""
        try:
            return str(Path(path).relative_to(REPO_ROOT))
        except ValueError:
            return str(path)

    @app.route("/api/locked-config", methods=["GET"])
    def locked_config_ep():
        """Read-only preview of the locked, validated study config (reference)."""
        path = config_builder.LOCKED_STUDY_CONFIG
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return jsonify({"error": "locked study config not found"}), 404
        return jsonify({
            "name": path.name,
            "path": _rel(path),
            "yaml": text,
            "locked": True,
            "note": "Validated study config — read-only reference, not editable here.",
        })

    @app.route("/api/build-config", methods=["POST", "OPTIONS"])
    def build_config_ep():
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        try:
            path, config = config_builder.build_and_write(
                name=body.get("name", ""),
                models=body.get("models"),
                dataset_path=body.get("dataset_path", ""),
                weights=body.get("weights"),
                targets=body.get("targets"),
                tiers=body.get("tiers"),
                agent_tokens=body.get("agent_tokens"),
                provider=body.get("provider", "hf"),
                overwrite=bool(body.get("overwrite", True)),
            )
        except config_builder.ConfigBuildError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "saved": True,
            "path": _rel(path),
            "name": path.name,
            "yaml": config_builder.to_yaml(config),
            "config": config,
        })

    @app.route("/api/preview-config", methods=["POST", "OPTIONS"])
    def preview_config_ep():
        """Validate + render YAML WITHOUT writing (live preview / weight check)."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        try:
            config = config_builder.build_config(
                name=body.get("name", "preview"),
                models=body.get("models"),
                dataset_path=body.get("dataset_path", ""),
                weights=body.get("weights"),
                targets=body.get("targets"),
                tiers=body.get("tiers"),
                agent_tokens=body.get("agent_tokens"),
                provider=body.get("provider", "hf"),
            )
        except config_builder.ConfigBuildError as e:
            return jsonify({"error": str(e), "valid": False}), 400
        return jsonify({"valid": True, "yaml": config_builder.to_yaml(config),
                        "config": config})

    # Phase 28 — download a double-click runner (.bat/.sh) for a USER config. This
    # only GENERATES and returns a FILE; it never runs anything and never triggers a
    # run from the browser. Running happens when the user double-clicks the file.
    @app.route("/api/config-runner", methods=["GET"])
    def config_runner_ep():
        name = request.args.get("name", "")
        os_kind = (request.args.get("os", "win") or "win").lower()
        target = (request.args.get("target", "local") or "local").lower()
        try:
            if target == "remote":
                # Phase 29: run on a Vast/SSH GPU box. Reads .env LIVE for the SSH
                # target; the generated file embeds only host/port/user/key-path.
                from agentmeter.server import remote_runner
                filename, content = remote_runner.remote_runner_script(name, os_kind)
            else:
                filename, content = config_builder.runner_script(name, os_kind)
        except config_builder.ConfigBuildError as e:
            return jsonify({"error": str(e)}), 400
        resp = Response(content, mimetype="application/octet-stream")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    @app.route("/api/vast-status", methods=["GET"])
    def vast_status_ep():
        """Readiness of the remote (Vast/SSH) runner from .env — PRESENCE only,
        never any secret value. Read live so an .env edit is reflected immediately."""
        from agentmeter.server import remote_runner
        return jsonify(remote_runner.ssh_status())

    @app.route("/api/vast-test", methods=["POST", "OPTIONS"])
    def vast_test_ep():
        """Pre-flight: a bounded, read-only SSH check (reachable + AgentMeter present).
        Runs NO benchmark. Reads .env live. Returns ok/message only, never a secret."""
        if request.method == "OPTIONS":
            return ("", 204)
        from agentmeter.server import remote_runner
        return jsonify(remote_runner.test_connection())

    # Phase 30 — Telegram notifications (new-analysis push).
    @app.route("/api/notify-status", methods=["GET"])
    def notify_status_ep():
        """Telegram readiness from .env — PRESENCE only, never the token value."""
        return jsonify(notify.notify_status())

    @app.route("/api/notify-test", methods=["POST", "OPTIONS"])
    def notify_test_ep():
        """Send a test message so the user can confirm the bot token + chat id work."""
        if request.method == "OPTIONS":
            return ("", 204)
        return jsonify(notify.send_telegram(
            "AgentMeter: test notification — your Telegram alerts are working."))

    @app.route("/api/notify-check", methods=["POST", "OPTIONS"])
    def notify_check_ep():
        """Scan results/ once and notify for any new analysis (also runs on a timer)."""
        if request.method == "OPTIONS":
            return ("", 204)
        return jsonify({"new": results_watcher.poll_once()})

    # --- dashboard (same-origin) ---------------------------------------
    @app.route("/", methods=["GET"])
    def index():
        return send_from_directory(dashboard_dir, "index.html")

    @app.route("/<path:asset>", methods=["GET"])
    def dashboard_asset(asset):
        if asset not in _ALLOWED_ASSETS:
            return jsonify({"error": "not found"}), 404
        return send_from_directory(dashboard_dir, asset)

    # --- API -----------------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"ok": True})

    @app.route("/pull-eval", methods=["POST", "OPTIONS"])
    def pull_eval_ep():
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        model_id = (body.get("model_id") or "").strip()
        if not model_id:
            return jsonify({"error": "model_id is required"}), 400
        # Validate size/type BEFORE downloading weights, and surface a clean 4xx.
        try:
            info = pull_eval.validate_model(
                model_id, max_params=manager.max_params,
                info_fetcher=manager.info_fetcher,
            )
        except ValueError as e:
            return jsonify({"error": str(e), "model_id": model_id}), 400
        except Exception as e:  # noqa: BLE001 — metadata fetch failure
            return jsonify({"error": f"could not fetch HF metadata: {e}",
                            "model_id": model_id}), 502
        # Single-job lock.
        try:
            status = manager.start(model_id)
        except RuntimeError as e:
            return jsonify({"error": str(e), "status": manager.status()}), 409
        return jsonify({"accepted": True, "model_info": info, "status": status})

    @app.route("/status", methods=["GET"])
    def status_ep():
        return jsonify(manager.status())

    @app.route("/settings-status", methods=["GET"])
    def settings_status_ep():
        # STATUS ONLY — booleans (set/not-set) and cost modes. NEVER a token value.
        tokens = {var: envtools.token_present(var) for var in envtools.TOKENS}
        auto = bool(guard.auto_destroy) if guard is not None else False
        idle_min = (guard.idle_timeout / 60.0) if guard is not None else 0.0
        return jsonify({
            "tokens": tokens,                       # {"HF_TOKEN": true/false, ...}
            "cost_safety": {"auto_destroy": auto, "idle_timeout_min": idle_min},
            "note": "Tokens are configured via the server-side .env file; their "
                    "values are never sent to or shown in the browser.",
        })

    @app.route("/analysis", methods=["GET"])
    def analysis_ep():
        payload = manager.analysis_payload()
        if payload is None:
            return jsonify({"error": "no completed pull/eval yet",
                            "status": manager.status()}), 404
        return jsonify(payload)

    # Phase 30 — background poller: notify on new analyses even when no browser is
    # open (so the phone push reaches you while you are away). Daemon thread; a
    # failure in a poll never takes down the server.
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


def _open_tunnel(port: int) -> str:
    """Open an ngrok tunnel and return the public URL (never hard-coded)."""
    from pyngrok import ngrok

    token = os.environ.get("NGROK_AUTHTOKEN")
    if token:
        ngrok.set_auth_token(token)
    tunnel = ngrok.connect(port, "http")
    return tunnel.public_url


def _local_ip() -> str:
    """Best-effort primary IPv4 of this host (for the reachable URL hint)."""
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


def _print_startup(port: int) -> None:
    """Print the exact URLs to open. On Vast.ai, the public address/port come from
    the instance's port mapping (env vars if present), so surface those too."""
    ip = _local_ip()
    # Vast.ai exposes the public address + external port via env when a port is mapped.
    pub_ip = os.environ.get("PUBLIC_IPADDR") or os.environ.get("VAST_PUBLIC_IPADDR")
    ext_port = (os.environ.get(f"VAST_TCP_PORT_{port}")
                or os.environ.get("VAST_TCP_PORT_8000"))
    print("=" * 72)
    print("  AgentMeter pull/eval server is LIVE  (dashboard + API, same origin)")
    print(f"  Local     : http://0.0.0.0:{port}/   (open http://{ip}:{port}/ )")
    if pub_ip and ext_port:
        print(f"  Vast.ai   : http://{pub_ip}:{ext_port}/   (mapped external port)")
    else:
        print("  Vast.ai   : open http://<instance-ip>:<mapped-port>/  (see the")
        print("              instance's port mappings in the Vast.ai console)")
    print("  The dashboard is served here; its API calls are same-origin — no CORS.")
    print("=" * 72, flush=True)


def _log_safety_modes(args, instance_id) -> None:
    """State plainly, at startup, which cost-safety modes are active."""
    print("-" * 72)
    if not args.auto_destroy:
        print("  Cost safety : auto-destroy OFF (dev/test mode — never self-destroys).")
        print("                Remember to set a scheduled end when you RENT the box.")
    else:
        have_key = bool(vast_shutdown.get_api_key())
        idle = ("disabled" if args.idle_timeout <= 0
                else f"{args.idle_timeout:.0f} min")
        print(f"  Cost safety : auto-destroy ON  (destroy after a run; idle timeout {idle}).")
        print(f"                instance id : {instance_id or 'UNKNOWN'} | "
              f"VAST_API_KEY : {'present' if have_key else 'MISSING'}")
        if not have_key or not instance_id:
            print("  WARNING     : credentials/instance id missing — self-destroy will "
                  "NO-OP. Set VAST_API_KEY and the instance id, and always set a "
                  "scheduled end on Vast as the outer safety net.")
    print("-" * 72, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="AgentMeter pull/eval server (Vast.ai): serves the dashboard "
                    "and the pull/eval API from one origin.")
    ap.add_argument("--config", default=str(pull_eval.DEFAULT_BASE_CONFIG),
                    help="base study config to derive pull runs from")
    ap.add_argument("--canonical", default=str(pull_eval.DEFAULT_CANONICAL_JSON),
                    help="locked canonical analysis.json (read-only, for merging)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--n", type=int, default=None,
                    help="cap scenarios (debug only; omit for the full 300)")
    ap.add_argument("--ngrok", action="store_true",
                    help="also open a public ngrok tunnel (optional; Vast.ai's "
                         "mapped port is usually directly reachable without it)")
    ap.add_argument("--auto-destroy", action="store_true",
                    help="DESTROY this Vast.ai instance after a run completes / on "
                         "idle timeout (cost safety). DEFAULT OFF — never destroys "
                         "while developing.")
    ap.add_argument("--idle-timeout", type=float, default=30.0,
                    help="minutes of inactivity (and no running job) before "
                         "self-destroy when --auto-destroy is set; 0 disables.")
    ap.add_argument("--instance-id", default=None,
                    help="Vast.ai instance id to destroy (else read from "
                         "VAST_INSTANCE_ID / Vast env vars).")
    ap.add_argument("--app-db", default=None,
                    help="metadata DB for CRUD (sessions/presets/notes); default "
                         "results/agentmeter_app.db (separate from the locked study DB).")
    ap.add_argument("--results-dir", default=None,
                    help="directory scanned READ-ONLY for local result JSON files "
                         "(default results/).")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Load tokens from .env (if present) so I don't paste them each run. Existing
    # environment values win; nothing is hard-coded and no value is ever logged.
    envtools.load_env()
    envtools.log_token_status()

    canonical = Path(args.canonical)
    if not canonical.exists():
        print(f"WARNING: canonical analysis not found at {canonical}. /analysis will "
              "fail until the locked study analysis.json exists.", file=sys.stderr)

    instance_id = args.instance_id or vast_shutdown.get_instance_id()
    guard = CostGuard(manager=None, auto_destroy=args.auto_destroy,
                      idle_timeout_min=args.idle_timeout, instance_id=instance_id)
    manager = pull_eval.JobManager(
        base_config=args.config, canonical_json=args.canonical, n=args.n,
        on_complete=guard.on_complete,
    )
    guard.manager = manager
    # Phase 40: build the job manager at startup so it recovers the store now —
    # jobs left running become "interrupted", queued jobs start without waiting
    # for the first API call.
    from agentmeter.server.jobs import JobManager
    job_manager = JobManager()
    app = create_app(manager, guard=guard, app_db_path=args.app_db,
                     local_results_dir=args.results_dir, start_watcher=True,
                     job_manager=job_manager)

    _print_startup(args.port)
    _log_safety_modes(args, instance_id)

    if args.auto_destroy and args.idle_timeout > 0:
        stop = threading.Event()
        threading.Thread(target=guard.watchdog, args=(stop,), daemon=True).start()

    if args.ngrok:
        if not app.config.get("AUTH_ENABLED"):
            print("\n" + "!" * 72, file=sys.stderr)
            print("  WARNING: opening a PUBLIC tunnel with NO authentication.", file=sys.stderr)
            print("  Anyone with the URL can read your results AND trigger /pull-eval", file=sys.stderr)
            print("  (which SPENDS GPU money). Set AGENTMETER_AUTH_USER / "
                  "AGENTMETER_AUTH_PASS", file=sys.stderr)
            print("  in .env to require a login. See docs/DEPLOY.md.", file=sys.stderr)
            print("!" * 72 + "\n", file=sys.stderr)
        try:
            url = _open_tunnel(args.port)
            print(f"  ngrok tunnel : {url}  (optional public URL)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"ngrok tunnel failed ({e}); the direct URL above still works.",
                  file=sys.stderr)

    # Bind 0.0.0.0 so the Vast.ai port mapping can reach it.
    app.run(host="0.0.0.0", port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
