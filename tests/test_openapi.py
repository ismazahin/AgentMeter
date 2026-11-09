"""The API documents (web/docs/openapi-*.json, from scripts/build_openapi.py) describe exactly the
routes the code serves — no more, no fewer — and are current; /health reports the API version."""
from __future__ import annotations

import importlib.util
import json
import re

from agentmeter.config import PROJECT_ROOT
from agentmeter.server.apiversion import API_VERSION


def _mod(name, rel):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


B = _mod("build_openapi", "scripts/build_openapi.py")
SKEL = lambda path: re.sub(r"\{[^}]+\}", "{}", path)                                    # noqa: E731
UI_ROUTES = {"/", "/{}", "/app.html", "/landing.html", "/analysis", "/baseline", "/config.json", "/prep/{}", "/service", "/service/benchmark", "/service/prepare", "/docs/", "/docs/{}", "/js/{}"}


def documented(server):
    return {(m.upper(), SKEL(path)) for path, ops in B.build(server)["paths"].items() for m in ops}


def test_the_control_plane_routes_match_the_document():
    src = (PROJECT_ROOT / "supabase/functions/agentmeter/src/app.ts").read_text()
    code = set()
    for m, rx in re.findall(r'\[\s*"(GET|POST|PUT|PATCH|DELETE)",\s*/\^(.*?)\$/', src):
        path = re.sub(r"\((?:[^()]|\([^()]*\))*\)", "{}", rx.replace("\\/", "/").replace("\\.", "."))
        code.add((m, path))
    assert code == documented(B.CP), f"in code only: {sorted(code - documented(B.CP))}; documented only: {sorted(documented(B.CP) - code)}"


def test_the_gpu_backend_routes_match_the_document(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMETER_RESULTS_DIR", str(tmp_path))
    serve = _mod("serve_for_openapi", "scripts/serve.py")
    app = serve.create_app(local_results_dir=tmp_path, app_db_path=tmp_path / "app.db")
    code = set()
    for r in app.url_map.iter_rules():
        path = SKEL(re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", r.rule))
        if path in UI_ROUTES:
            continue
        for m in r.methods - {"HEAD", "OPTIONS"}:
            code.add((m, path))
    assert code == documented(B.BE), f"in code only: {sorted(code - documented(B.BE))}; documented only: {sorted(documented(B.BE) - code)}"


def test_the_cloud_prep_routes_match_the_document():
    src = (PROJECT_ROOT / "cloudprep/app.py").read_text()
    code = {("GET", p) for p in re.findall(r'if self\.path == "([^"]+)"', src)} | {("POST", p) for p in re.findall(r'if self\.path != "([^"]+)"', src)}
    assert code == documented(B.PREP)


def test_the_documents_are_current_and_valid_openapi():
    for name, text in B.exported().items():
        on_disk = (PROJECT_ROOT / "web" / "docs" / name).read_text(encoding="utf-8")
        assert on_disk == text, "run: python scripts/build_openapi.py"
        d = json.loads(text)
        assert d["openapi"] == "3.1.0" and d["info"]["version"] == API_VERSION
        for path, ops in d["paths"].items():
            for m, o in ops.items():
                assert o["summary"] and o["tags"] and "responses" in o, (path, m)
                for prm in re.findall(r"\{([^}]+)\}", path):
                    assert any(x["name"] == prm and x["in"] == "path" for x in o.get("parameters", [])), (path, m, prm)


def test_the_control_plane_reports_the_same_api_version():
    src = (PROJECT_ROOT / "supabase/functions/agentmeter/src/app.ts").read_text()
    assert f'export const API_VERSION = "{API_VERSION}";' in src


def test_health_reports_the_api_version(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMETER_RESULTS_DIR", str(tmp_path))
    serve = _mod("serve_for_health", "scripts/serve.py")
    app = serve.create_app(local_results_dir=tmp_path, app_db_path=tmp_path / "app.db")
    assert app.test_client().get("/health").get_json()["api_version"] == API_VERSION
