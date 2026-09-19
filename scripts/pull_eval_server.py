"""Phase 11 — admin model-pull + on-demand evaluation SERVER (Colab GPU host).

A tiny Flask app, tunnelled with ngrok, that lets the dashboard pull ONE extra HF
model and run it through the EXISTING harness for a side-by-side comparison,
without touching the locked 5-model study. ALL the heavy lifting and every
integrity rule live in agentmeter/pull_eval.py — this file is just the HTTP
surface + the ngrok tunnel.

Run on the GPU host (e.g. Colab), AFTER the locked study analysis.json exists:

    pip install -r requirements-gpu.txt          # includes flask + pyngrok
    export NGROK_AUTHTOKEN=...                    # your ngrok token
    export HF_TOKEN=...                           # for gated models
    python scripts/pull_eval_server.py

It prints the public ngrok URL. Paste that into the dashboard's admin panel
(dashboard/pull-config.js) — it is NEVER hard-coded anywhere.

Endpoints (JSON):
    POST /pull-eval   {"model_id": "..."}  -> start a run (validates size FIRST)
    GET  /status                            -> {state, model_id, progress, scenario, error}
    GET  /analysis                          -> merged canonical + exploratory analysis
    GET  /health                            -> liveness
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow "python scripts/pull_eval_server.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmeter import pull_eval  # noqa: E402


def _cors(resp):
    """The dashboard is opened from file:// (or a different origin), so allow it."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def create_app(manager: "pull_eval.JobManager"):
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.after_request
    def after(resp):  # noqa: ANN001
        return _cors(resp)

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentMeter pull/eval server (ngrok).")
    ap.add_argument("--config", default=str(pull_eval.DEFAULT_BASE_CONFIG),
                    help="base study config to derive pull runs from")
    ap.add_argument("--canonical", default=str(pull_eval.DEFAULT_CANONICAL_JSON),
                    help="locked canonical analysis.json (read-only, for merging)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--n", type=int, default=None,
                    help="cap scenarios (debug only; omit for the full 300)")
    ap.add_argument("--no-ngrok", action="store_true",
                    help="serve locally without opening an ngrok tunnel")
    args = ap.parse_args(argv)

    canonical = Path(args.canonical)
    if not canonical.exists():
        print(f"WARNING: canonical analysis not found at {canonical}. /analysis will "
              "fail until the locked study analysis.json exists.", file=sys.stderr)

    manager = pull_eval.JobManager(
        base_config=args.config, canonical_json=args.canonical, n=args.n
    )
    app = create_app(manager)

    if not args.no_ngrok:
        try:
            url = _open_tunnel(args.port)
            print("=" * 70)
            print("  AgentMeter pull/eval server is LIVE")
            print(f"  Public ngrok URL : {url}")
            print("  Paste this into the dashboard admin panel (pull-config.js).")
            print("=" * 70, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"ngrok tunnel failed ({e}); serving locally on :{args.port} only.",
                  file=sys.stderr)

    app.run(host="0.0.0.0", port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
