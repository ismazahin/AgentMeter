"""Phase 16 — local result-file discovery for the dashboard (READ-ONLY).

Lists analysis JSON files already present under results/ so the Sessions tab can
load them without a manual file picker, and reads one by name for rendering.

Strictly read-only discovery: it never writes, moves, or deletes anything, never
opens the locked study DB, and only ever reads *.json files that resolve to
INSIDE the configured results/ directory. Path traversal / absolute paths /
symlink escapes are rejected.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LocalSessions:
    def __init__(self, results_dir: str | Path):
        # The allow-list root. Everything served must resolve inside this.
        self.root = Path(results_dir).resolve()

    # --- listing --------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        """Every *.json file under results/ (recursive), newest first. Returns
        metadata only (name/size/modified) — never file contents."""
        out: list[dict[str, Any]] = []
        if not self.root.exists() or not self.root.is_dir():
            return out
        for p in self.root.rglob("*.json"):
            try:
                if not p.is_file():
                    continue
                rp = p.resolve()
                rel = rp.relative_to(self.root)     # skip anything escaping root
            except (ValueError, OSError):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": str(rel).replace(os.sep, "/"),
                "size": int(st.st_size),
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                    .isoformat(timespec="seconds"),
            })
        out.sort(key=lambda x: x["modified"], reverse=True)
        return out

    # --- safe read by name ---------------------------------------------
    def resolve_safe(self, name: str) -> Path:
        """Resolve `name` to a real .json file strictly inside results/, or raise.
        Rejects empty/absolute names, `..` traversal, and symlink escapes."""
        if not name or not isinstance(name, str):
            raise ValueError("a file name is required")
        n = name.strip().replace("\\", "/")
        parts = Path(n).parts
        if n.startswith("/") or ".." in parts or any(p in ("", ".") for p in parts):
            raise ValueError("invalid file name (no absolute paths or '..')")
        if Path(n).is_absolute() or (len(n) > 1 and n[1] == ":"):   # drive letters too
            raise ValueError("absolute paths are not allowed")

        cand = (self.root / n).resolve()
        if not (cand == self.root or self._within(cand)):
            raise ValueError("path escapes the results/ directory")
        if cand.suffix.lower() != ".json":
            raise ValueError("only .json result files can be read")
        if not cand.is_file():
            raise FileNotFoundError(name)
        return cand

    def _within(self, p: Path) -> bool:
        try:
            p.relative_to(self.root)
            return True
        except ValueError:
            return False

    def read_json(self, name: str) -> Any:
        """Parse and return the JSON of a results/ file (read-only)."""
        p = self.resolve_safe(name)
        return json.loads(p.read_text(encoding="utf-8"))
