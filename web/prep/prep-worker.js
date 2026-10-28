// "Prepare on this computer": the dataset preparation runs HERE, on the user's own CPU, in this
// Web Worker, with Pyodide (Python compiled to WebAssembly). It runs the SAME Python code as the
// GPU server (agentmeter.server.prepare.prepare_file, bundled in agentmeter-prep.zip by
// scripts/build_web_prep.py), so the data files are byte-identical to a server preparation. The
// GPU server only receives the small prepared set (manifest.json, features.csv, labels.csv),
// checks its hashes and stores it at once, even while it is busy benchmarking.
//
// Messages in:  {type: "prepare", base: <Pyodide URL, ending in />, file: File, max_flows, other_attack, limits}
// Messages out: {type: "progress", phase, message} ... then {type: "done", set_name, files: {name: ArrayBuffer}}
//               or {type: "error", error, code}
const PYODIDE_PACKAGES = ["numpy", "pandas", "scipy", "pyyaml", "micropip"];
const WHEELS = ["scapy-2.8.0-py3-none-any.whl", "cicflowmeter-0.2.0-py3-none-any.whl"];   // pure Python (Scapy: GPL-2.0, CICFlowMeter port: MIT)
const HERE = new URL(".", self.location.href).href;
let ready = null;

const post = (m, transfer) => self.postMessage(m, transfer || []);

// Browser-only shims, applied before Scapy loads. Nothing here touches the network: Scapy only
// parses the capture file. Pyodide has no IPv6 sockets, so Scapy's own pure-Python IPv6 address
// conversion stands in for the socket functions; cicflowmeter imports `requests` for an HTTP output
// writer that is never used.
const SHIMS = `
import socket, sys, types
socket.has_ipv6 = True
_pton, _ntop = socket.inet_pton, socket.inet_ntop
def _pton6(af, a):
    try:
        return _pton(af, a)
    except (OSError, ValueError):
        if af != socket.AF_INET6:
            raise
        from scapy.pton_ntop import _inet6_pton
        return _inet6_pton(a)
def _ntop6(af, a):
    try:
        return _ntop(af, a)
    except (OSError, ValueError):
        if af != socket.AF_INET6:
            raise
        from scapy.pton_ntop import _inet6_ntop
        return _inet6_ntop(a)
socket.inet_pton, socket.inet_ntop = _pton6, _ntop6
sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.path.insert(0, "/app")
`;

const PREPARE = `
import json, shutil
from pathlib import Path
from agentmeter.server import prepare as P
a = dict(_args)
root = Path("/work")
shutil.rmtree(root, ignore_errors=True)
src = Path("/input") / a["filename"]
def report(**kw):
    _report(json.dumps(kw, default=str))
report(phase="validating", message="checking " + a["filename"])
source = {"kind": "browser", "filename": a["filename"], "size_bytes": src.stat().st_size, "sha256": P.sha256_file(src)}
env = {"provider": "browser", "prepared_on": "the user's computer (browser)", "runtime": a["runtime"]}
try:
    sid = P.prepare_file(src, source=source, results_root=root, name=P.unique_name(a["filename"]),
                         max_flows=int(a["max_flows"]), other_attack=bool(a["other_attack"]),
                         limits=a["limits"], progress=report, environment=env)
    out = json.dumps({"ok": True, "dir": str(root / sid), "set_name": sid})
except P.PrepareError as e:
    out = json.dumps({"ok": False, "error": str(e), "code": e.code})
out
`;

function boot(base) {
  if (ready) return ready;
  ready = (async () => {
    post({ type: "progress", phase: "loading", message: "Loading Python in your browser (the first time downloads about 40 MB; then it is cached)" });
    const { loadPyodide } = await import(base + "pyodide.mjs");
    const py = await loadPyodide({ indexURL: base });
    post({ type: "progress", phase: "loading", message: "Loading pandas, NumPy and SciPy" });
    await py.loadPackage(PYODIDE_PACKAGES);
    post({ type: "progress", phase: "loading", message: "Loading the AgentMeter preparation code and Scapy" });
    const zip = await fetch(HERE + "agentmeter-prep.zip");
    if (!zip.ok) throw new Error("could not load agentmeter-prep.zip (" + zip.status + ")");
    py.unpackArchive(await zip.arrayBuffer(), "zip", { extractDir: "/app" });
    py.globals.set("_wheels", py.toPy(WHEELS.map((w) => HERE + "wheels/" + w)));
    await py.runPythonAsync("import micropip\nawait micropip.install(list(_wheels), deps=False)");
    await py.runPythonAsync(SHIMS);
    return py;
  })();
  ready.catch(() => { ready = null; });            // a failed load can be retried
  return ready;
}

self.onmessage = async (ev) => {
  const m = ev.data || {};
  if (m.type !== "prepare") return;
  try {
    const py = await boot(m.base);
    try { py.FS.unmount("/input"); } catch (e) { /* not mounted yet */ }
    py.FS.mkdirTree("/input");
    py.FS.mount(py.FS.filesystems.WORKERFS, { files: [m.file] }, "/input");   // read in place: no copy of the file
    py.globals.set("_report", (s) => { try { post(Object.assign({ type: "progress" }, JSON.parse(s))); } catch (e) { /* ignore */ } });
    py.globals.set("_args", py.toPy({ filename: m.file.name, max_flows: m.max_flows, other_attack: !!m.other_attack,
                                      limits: m.limits, runtime: "Pyodide " + py.version }));
    const res = JSON.parse(await py.runPythonAsync(PREPARE));
    if (!res.ok) { post({ type: "error", error: res.error, code: res.code }); return; }
    const files = {}, transfer = [];
    for (const name of ["manifest.json", "features.csv", "labels.csv"]) {
      const p = res.dir + "/" + name;
      if (py.FS.analyzePath(p).exists) { const b = py.FS.readFile(p); files[name] = b.buffer; transfer.push(b.buffer); }
    }
    post({ type: "done", set_name: res.set_name, files }, transfer);
  } catch (e) {
    post({ type: "error", error: "Preparing on this computer failed: " + String((e && e.message) || e).split("\n").slice(-3).join(" ").slice(0, 400), code: "browser_prepare_failed" });
  }
};
