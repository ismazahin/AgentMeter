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
import os
import socket
import sys
from pathlib import Path

# Allow "python scripts/pull_eval_server.py" from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentmeter import pull_eval  # noqa: E402

DASHBOARD_DIR = REPO_ROOT / "dashboard"
# Only these dashboard assets are servable (no arbitrary file access).
_ALLOWED_ASSETS = {
    "index.html", "saw.js", "pull-config.js", "sample_analysis.json", "README.md",
}


def _manual_cors(resp):
    """Fallback permissive CORS (used only if flask-cors is not installed), so a
    file:// dashboard (origin "null") can still reach the API."""
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return resp


def create_app(manager: "pull_eval.JobManager", dashboard_dir: Path = DASHBOARD_DIR):
    from flask import Flask, jsonify, request, send_from_directory

    app = Flask(__name__, static_folder=None)

    # Prefer flask-cors; fall back to manual headers if it is not installed. The
    # dashboard is served SAME-ORIGIN from this app, so CORS is only a fallback
    # for the file:// case.
    try:
        from flask_cors import CORS

        CORS(app, resources={r"/pull-eval": {"origins": "*"},
                             r"/status": {"origins": "*"},
                             r"/analysis": {"origins": "*"},
                             r"/health": {"origins": "*"}})
    except Exception:  # noqa: BLE001 — flask-cors optional
        @app.after_request
        def after(resp):  # noqa: ANN001
            return _manual_cors(resp)

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

    @app.route("/analysis", methods=["GET"])
    def analysis_ep():
        payload = manager.analysis_payload()
        if payload is None:
            return jsonify({"error": "no completed pull/eval yet",
                            "status": manager.status()}), 404
        return jsonify(payload)

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
    args = ap.parse_args(argv)

    canonical = Path(args.canonical)
    if not canonical.exists():
        print(f"WARNING: canonical analysis not found at {canonical}. /analysis will "
              "fail until the locked study analysis.json exists.", file=sys.stderr)

    manager = pull_eval.JobManager(
        base_config=args.config, canonical_json=args.canonical, n=args.n
    )
    app = create_app(manager)

    _print_startup(args.port)
    if args.ngrok:
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
