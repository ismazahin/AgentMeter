"""Serve web/ like Cloudflare Pages for the local walk-throughs: applies web/_headers (so the
strict CSP is exercised — a stale script hash breaks the page here, as it would on Pages) and
serves config.json from the command line.

    python tests/e2e/pages_server.py <port> <control_plane_url> [extra connect-src origins...]
"""
from __future__ import annotations

import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def pages_headers(extra_connect: list[str]) -> dict[str, str]:
    hdrs, block = {}, None
    for line in (WEB / "_headers").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" "):
            block = line.strip()
            continue
        if block == "/*":
            k, v = line.strip().split(":", 1)
            hdrs[k.strip()] = v.strip()
    csp = hdrs["Content-Security-Policy"]
    hdrs["Content-Security-Policy"] = csp.replace("connect-src 'self'", "connect-src 'self' " + " ".join(extra_connect))
    return hdrs


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, cfg=None, hdrs=None, **kw):
        self.cfg, self.hdrs = cfg, hdrs
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, *a):  # noqa: ANN002
        pass

    def end_headers(self):
        for k, v in self.hdrs.items():
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/config.json":
            b = json.dumps(self.cfg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        super().do_GET()


if __name__ == "__main__":
    port, cp_url = int(sys.argv[1]), sys.argv[2]
    extra = [cp_url] + sys.argv[3:]
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(H, cfg={"api_base": "", "control_plane": cp_url},
                                                           hdrs=pages_headers(extra)))
    print(f"pages on http://127.0.0.1:{port} (control plane {cp_url})", flush=True)
    srv.serve_forever()
