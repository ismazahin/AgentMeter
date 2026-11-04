"""The AgentMeter API version (semantic versioning), reported by GET /health and written into the
OpenAPI documents (scripts/build_openapi.py). Raise MINOR for a new endpoint or field; raise MAJOR
for a removed or changed one (keep the old one for a while, marked deprecated). The web app warns
when the GPU backend reports an older MINOR than it needs (MIN_BACKEND in web/index.html).

History: 1.0 the Phase 47 split · 1.1 GET /api/gpu, GET /api/logs/recent, offset/next_offset paging · 1.2 triage results (session "triage" block, flow "priority" / "truth_priority") · 1.3 severity and the triage gate (triage "severity", "gate"; flow "severity") · 1.4 the IDS gate (job "ids", GET /health "ids", session "ids", flow "ids_score").
"""
API_VERSION = "1.4.0"
