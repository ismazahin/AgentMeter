"""Run the web app's own insight rules (web/insights.js) from Python, for the PDF report.

The session pages explain every number with plain sentences written by `web/insights.js`
(rule-based, node-tested in tests/test_insights.py). The PDF calls the very same file through an
embedded QuickJS engine, so the report and the website always say the same thing about a session
and a rule is written once. Nothing here measures or scores anything: every function only re-reads
the stored session_results.json.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_INSIGHTS = Path(__file__).resolve().parents[2] / "web" / "insights.js"


class InsightsUnavailable(RuntimeError):
    """The JS engine or web/insights.js is missing: the caller falls back to the plain report."""


class Insights:
    """INSIGHTS.<fn>(session, ...) for one session. Each call returns plain JSON data."""

    def __init__(self, res: dict[str, Any], source: Path = _INSIGHTS):
        try:
            import quickjs
        except ImportError as e:  # pragma: no cover - dependency missing on the server
            raise InsightsUnavailable(f"quickjs is not installed: {e}") from e
        if not source.exists():
            raise InsightsUnavailable(f"{source} not found")
        self._ctx = quickjs.Context()
        self._ctx.eval(source.read_text(encoding="utf-8"))
        self._ctx.eval("var R = " + json.dumps(res, default=str) + ";")
        self.models = [m["model"] for m in res.get("per_model") or []]

    def call(self, fn: str, *args: Any, default: Any = None, with_res: bool = True) -> Any:
        """INSIGHTS.fn(R, *args) as Python data; `default` when the rule throws on this session.
        with_res=False calls INSIGHTS.fn(*args) for the few rules that take one row, not the session."""
        js_args = ", ".join((["R"] if with_res else []) + [json.dumps(a) for a in args])
        try:
            out = self._ctx.eval(f"JSON.stringify(INSIGHTS.{fn}({js_args}))")
        except Exception:  # a malformed or older block: the section shows what it can
            return default
        return default if out is None else json.loads(out)

    def plain(self, text: Any) -> str:
        """The web's plain-words pass: everyday model names, no em dashes, no mid-sentence semicolons."""
        if text is None:
            return ""
        return self._ctx.eval(f"INSIGHTS.plainText({json.dumps(str(text))}, {json.dumps(self.models)})")

    def nick(self, model: Any) -> str:
        return self._ctx.eval(f"INSIGHTS.nick({json.dumps(str(model or ''))})")
