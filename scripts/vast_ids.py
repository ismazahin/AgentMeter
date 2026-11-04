#!/usr/bin/env python3
"""Fetch the IDS gate's model from the control plane at GPU start-up (scripts/vast_up.sh).

An admin uploads the model once (Admin › IDS model on the site); every newly rented GPU then
downloads it here, so nothing is copied by hand. The files land in <out> (default
$AGENTMETER_DATA_DIR/ids or models/ids) and are checked by loading them (sha256 against the
metadata, scikit-learn version). Prints the path to export as AGENTMETER_IDS_MODEL on success.

    python scripts/vast_ids.py                  # needs AGENTMETER_CONTROL_PLANE_URL + AGENTMETER_BACKEND_SECRET
    python scripts/vast_ids.py --out /root/ids

Exit codes: 0 = fetched and verified (the .joblib path is the last line printed);
            3 = no model stored (the server runs with the demo IDS only); 1 = an error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentmeter.server.control_plane import Client, ControlPlaneError, cp_url  # noqa: E402

NAMES = ("ids_rf.joblib", "ids_rf.json", "ids_rf.hashes.npy")


def fetch(client: Client, out: Path) -> Path:
    d = json.loads(client.request("GET", "/api/backend/ids-model"))
    out.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        url = (d.get("files") or {}).get(name)
        if not url:
            continue
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "store.test")):
            raise ControlPlaneError(f"refusing a non-https download URL for {name}")
        tmp = out / (name + ".part")
        with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as fh:  # noqa: S310 — checked above
            while chunk := r.read(1 << 20):
                fh.write(chunk)
        tmp.replace(out / name)
    return out / "ids_rf.joblib"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    data = os.environ.get("AGENTMETER_DATA_DIR")
    ap.add_argument("--out", type=Path, default=Path(data) / "ids" if data else REPO / "models" / "ids")
    a = ap.parse_args(argv)
    url = cp_url()
    secret = os.environ.get("AGENTMETER_BACKEND_SECRET", "").strip()
    if not url or not secret:
        print("IDS model: AGENTMETER_CONTROL_PLANE_URL / AGENTMETER_BACKEND_SECRET not set — skipped", file=sys.stderr)
        return 1
    try:
        path = fetch(Client(url, secret, timeout=30), a.out)
    except ControlPlaneError as e:
        if getattr(e, "code", "") == "no_ids_model":
            print("IDS model: none stored on the control plane (Admin › IDS model) — the demo IDS only")
            return 3
        print(f"IDS model: could not fetch it ({e})", file=sys.stderr)
        return 1
    from agentmeter.ids.model import IdsError, load_model
    try:
        m = load_model(path)
    except IdsError as e:
        for n in NAMES:                       # never leave a model the server would refuse
            (a.out / n).unlink(missing_ok=True)
        print(f"IDS model: downloaded but refused ({e})", file=sys.stderr)
        return 1
    print(f"IDS model: {m.meta.get('version')} (threshold {m.threshold:.4f}) ready")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
