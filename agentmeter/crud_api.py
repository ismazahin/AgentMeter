"""Phase 12 — CRUD REST routes for sessions, weight presets, and notes.

A thin JSON/HTTP wrapper over agentmeter.appdb.AppStore, registered onto the
existing Flask app (same-origin; the app's CORS fallback already covers these
routes). All business rules and validation live in appdb; this file only maps
HTTP verbs to store methods and turns store exceptions into clean status codes.

Scope: sessions + metadata, SAW weight presets, notes/annotations ONLY. No CRUD
on datasets or models; imported analysis data is read-only once stored; the locked
study DB is never touched.
"""
from __future__ import annotations

from typing import Any, Callable

from . import appdb


def register_crud(app, get_store: Callable[[], "appdb.AppStore"]) -> None:
    """Add the CRUD routes to `app`. `get_store` returns an AppStore lazily (so the
    metadata DB is created only when a CRUD endpoint is first used)."""
    from flask import jsonify, request

    def body() -> dict[str, Any]:
        return request.get_json(silent=True) or {}

    def err(status: int, message: str):
        return jsonify({"error": message}), status

    # Map store exceptions -> HTTP status.
    def handle(fn):
        try:
            return fn()
        except appdb.NotFound as e:
            return err(404, str(e))
        except appdb.Forbidden as e:
            return err(403, str(e))
        except ValueError as e:
            return err(400, str(e))

    # ---------------- sessions ----------------
    @app.route("/sessions", methods=["GET", "POST"])
    def sessions_collection():
        if request.method == "GET":
            # optional ?tag=<id|name> filter
            tag = request.args.get("tag")
            return jsonify({"sessions": get_store().list_sessions(tag=tag or None)})
        b = body()
        return handle(lambda: (jsonify(get_store().create_session(
            name=b.get("name", ""),
            analysis=b.get("analysis"),
            source_filename=b.get("source_filename"),
            analysis_ref=b.get("analysis_ref"),
        )), 201))

    @app.route("/sessions/<int:sid>", methods=["GET", "PATCH", "DELETE"])
    def session_item(sid):
        if request.method == "GET":
            return handle(lambda: jsonify(get_store().get_session(sid, include_analysis=True)))
        if request.method == "PATCH":
            b = body()
            # ONLY the name may change — never the stored metrics/analysis.
            return handle(lambda: jsonify(get_store().update_session_name(sid, b.get("name", ""))))
        # DELETE
        def _del():
            get_store().delete_session(sid)
            return jsonify({"deleted": sid})
        return handle(_del)

    @app.route("/sessions/<int:sid>/notes", methods=["GET"])
    def session_notes(sid):
        return handle(lambda: jsonify({"notes": get_store().list_notes(sid)}))

    # ---------------- tags (Phase 19) ----------------
    @app.route("/tags", methods=["GET", "POST"])
    def tags_collection():
        if request.method == "GET":
            return jsonify({"tags": get_store().list_tags()})
        b = body()
        return handle(lambda: (jsonify(get_store().get_or_create_tag(b.get("name", ""))), 201))

    @app.route("/tags/<int:tid>", methods=["DELETE"])
    def tag_item(tid):
        def _del():
            get_store().delete_tag(tid)
            return jsonify({"deleted": tid})
        return handle(_del)

    @app.route("/sessions/<int:sid>/tags", methods=["GET", "POST"])
    def session_tags(sid):
        if request.method == "GET":
            return handle(lambda: jsonify({"tags": get_store().list_session_tags(sid)}))
        b = body()
        return handle(lambda: (jsonify(get_store().add_session_tag(sid, b.get("name", ""))), 201))

    @app.route("/sessions/<int:sid>/tags/<int:tid>", methods=["DELETE"])
    def session_tag_item(sid, tid):
        return handle(lambda: jsonify(get_store().remove_session_tag(sid, tid)))

    # ---------------- weight presets ----------------
    @app.route("/presets", methods=["GET", "POST"])
    def presets_collection():
        if request.method == "GET":
            return jsonify({"presets": get_store().list_presets()})
        b = body()
        return handle(lambda: (jsonify(get_store().create_preset(
            name=b.get("name", ""), weights=b)), 201))

    @app.route("/presets/<int:pid>", methods=["PATCH", "DELETE"])
    def preset_item(pid):
        if request.method == "PATCH":
            b = body()
            has_weights = any(k in b for k in appdb.WEIGHT_KEYS)
            return handle(lambda: jsonify(get_store().update_preset(
                pid, name=b.get("name"), weights=(b if has_weights else None))))
        def _del():
            get_store().delete_preset(pid)
            return jsonify({"deleted": pid})
        return handle(_del)

    # ---------------- notes ----------------
    @app.route("/notes", methods=["POST"])
    def notes_create():
        b = body()
        return handle(lambda: (jsonify(get_store().create_note(
            session_id=b.get("session_id"), body=b.get("body", ""))), 201))

    @app.route("/notes/<int:nid>", methods=["PATCH", "DELETE"])
    def note_item(nid):
        if request.method == "PATCH":
            b = body()
            return handle(lambda: jsonify(get_store().update_note(nid, b.get("body", ""))))
        def _del():
            get_store().delete_note(nid)
            return jsonify({"deleted": nid})
        return handle(_del)
