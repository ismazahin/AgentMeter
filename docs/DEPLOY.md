# Deploying AgentMeter (internet access, safely)

The backend is one Flask process (`scripts/serve.py`). It serves the benchmark service
API and the app at `/` (Home, New benchmark, Sessions, Compare, Leaderboard; `/service` is an
alias). The read-only Validation-baseline page stays at `/baseline` by direct URL only. The
front-end can also be hosted as static files on Vercel or Cloudflare Pages
(`web/`). Prepare and Benchmark spend GPU money, so a public backend must be protected.
The full GPU runbook is in "Runbook: static front-end + Vast.ai GPU backend" below.

## Security model (before any public exposure)

- **Access passcode.** Set `AGENTMETER_PASSCODE` to a long passphrase. Every
  state-changing request (Prepare, re-upload, Benchmark, resume, notify-test) and the
  job, session and leaderboard listings then need it; the page asks once per browser tab. A real-GPU server
  refuses to start without it.
- **Allowed origins.** Set `AGENTMETER_ALLOWED_ORIGINS` to your front-end's exact
  origin(s). A real-GPU server never answers other origins and never sends a CORS
  wildcard.
- **HTTPS.** Expose the server only through Cloudflare Tunnel (`scripts/vast_up.sh`
  sets it up). The server then listens on 127.0.0.1, so the tunnel is the only way in.
- **Rate limits.** Job creation is limited per client (`AGENTMETER_JOBS_PER_HOUR`) and
  the queue is capped (`AGENTMETER_MAX_QUEUED_JOBS`). Wrong passcodes are throttled.
- Read-only pages stay open: `/health`, the pages themselves, and a finished result or
  prepared set opened by its id.

For private use, a Tailscale network between your own devices also works: run
`python scripts/serve.py`, then open `http://<tailscale-ip>:8000/`.

## Uploads, URL import and where requests go

**Browser uploads go straight to the backend through the tunnel, never through a
Vercel function.** If the UI is also published on Vercel, that deployment is
static files only (HTML/JS). The page calls the backend's `/api/...` routes on the
tunnel origin directly. Vercel serverless functions cap a request body at about
4.5 MB, so no upload, prepared-set file or API call may be proxied through one (no
`rewrites` to the backend, no API route in front of it).

**Upload limit: 90 MB by default** (`config.yaml` `service.max_upload_mb`, or
`AGENTMETER_MAX_UPLOAD_MB`). Cloudflare limits a request body to **100 MB on the
Free and Pro plans** (200 MB Business, 500 MB Enterprise by default), and a Cloudflare
Tunnel is covered by the same limit. An upload above it fails at Cloudflare with an
HTML 413 before it reaches AgentMeter, so the default stays below it, and the page
refuses a larger file before sending it. Raise `max_upload_mb` only if your plan
allows larger bodies.

**Larger files: Import from URL.** The Prepare page accepts a direct `https://` link.
The server downloads the file itself, server to server, so nothing large crosses
the tunnel. Limits: `service.max_url_download_gb` (default 10 GB) and
`service.url_timeout_s` (default 3600 s). The size is refused up front from
Content-Length and enforced again while streaming. SSRF protection is always on:
https only, port 443, public addresses only (DNS checked on every redirect and the
connection pinned to the checked IP), at most 5 redirects. HTML pages (Google
Drive / Dropbox "share" links) are refused with a "use a direct download link"
message. If the server must reach the internet through an outbound proxy, URL
import does not use it (the IP pinning needs a direct connection).

**Large inputs** are sampled across the whole file: CSV with a stratified reservoir,
PCAP with evenly spaced time windows. Preparing runs as a background job, so no
request waits on ingestion. See docs/INPUT_LAYER.md. If a reverse proxy sits in
front, set its body limit to at least the upload limit (nginx
`client_max_body_size 91m;`).

**Test-only switches (never in production):** `AGENTMETER_TEST_ALLOW_LOOPBACK_URLS=1`
(plus `AGENTMETER_TEST_URL_CAFILE`) lets URL import fetch from a loopback https
server for the end-to-end test. They are read only from the server's environment,
not from any request or UI field, never open private or link-local ranges, and
log a warning when used.

## Runbook: static front-end + Vast.ai GPU backend (Phase E)

AgentMeter **measures** LLM resource efficiency (and accuracy where labels exist). It
is not a threat-detection product, and nothing below changes that.

```
browser ──(static files)──▶ Vercel / Cloudflare Pages      web/  (no functions)
   │
   └──(every API call, upload, download, PDF)──▶ https://<tunnel>  ─▶ cloudflared ─▶ 127.0.0.1:8000
                                                  Cloudflare Tunnel     on the Vast box: ONE server process,
                                                                        provider REAL, 5 models on disk
```

The front-end finds the backend through `web/config.json` (`api_base`), or through
`?api=<url>` for a one-off. When `/health` doesn't answer, the page shows a "GPU
backend offline" landing page, so it stays a working description of the service
while no GPU is rented. Before anyone clicks Run, the header badge says either
**Real GPU: <name>** or **DEMO (mock)**.

### 0. One-time preparation (on your laptop)
1. **Hugging Face:** with the account that owns your token, open
   https://huggingface.co/meta-llama/Meta-Llama-3-8B-Instruct and
   https://huggingface.co/google/gemma-2-9b-it and accept both licences. Wait until
   both pages say you have access (usually minutes). Create a **read** token at
   https://huggingface.co/settings/tokens.
2. **Passcode:** pick a long passphrase (12+ characters). Give it only to the people
   who may spend GPU time.
3. **Front-end:** deploy it now (step 4), so you know its origin, e.g.
   `https://agentmeter.vercel.app`.

### 1. Choose a Vast.ai instance
| need | value |
|---|---|
| GPU VRAM | **≥ 16 GB minimum, 24 GB recommended.** The largest model, gemma-2-9b, is about 6-7 GB in 4-bit NF4, plus KV cache and activations, one model at a time. |
| Recommended GPUs | **NVIDIA L4 24 GB** (same as the locked study, so the most comparable numbers), RTX 4090 / 3090 24 GB, RTX A5000 24 GB, L40S / A6000 48 GB |
| Disk | **≥ 150 GB.** The 5 models' safetensors are about 72 GB, plus Python/CUDA wheels about 10 GB, plus inputs (URL imports up to 10 GB each are kept under `results/uploads/`) |
| Image | a Vast **PyTorch** template with CUDA ≥ 12.1 (e.g. `pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime`) |
| Network | download ≥ 500 Mbps (72 GB of weights take about 20 min at that speed) |
| Ports | none: the tunnel dials **out**, so no open or mapped port is needed |
| Reliability | ≥ 98 %, on-demand (not interruptible: a preempted box loses the running job) |

Use "Instance → Connect → SSH" to get a shell on it.

### 2. Bring the backend up (on the instance)
```bash
cd /workspace
git clone https://github.com/ismazahin/AgentMeter.git && cd AgentMeter
git checkout claude/cool-ride-mitmzl          # or main once merged
export HF_TOKEN=hf_xxx
export AGENTMETER_PASSCODE='your long passphrase'
export AGENTMETER_ALLOWED_ORIGINS=https://agentmeter.vercel.app      # exact origin, no trailing slash
# optional cost guard: destroy the instance after 45 idle minutes (disk is lost!)
# export VAST_API_KEY=xxxx IDLE_MIN=45
bash scripts/vast_up.sh                         # quick tunnel; see step 3 for a named one
```
`vast_up.sh` checks the settings, installs dependencies, checks CUDA, then runs
`scripts/vast_models.py`: it checks access to all 5 models **before** downloading,
downloads them to `/workspace/hf`, and loads Phi-3 in 4-bit NF4 as a smoke test
(`VERIFY=all` loads all five). It then starts **one** server process in **real** mode
on 127.0.0.1:8000 and opens the tunnel. It prints the backend URL and the
`config.json` line to use.

The server **refuses to start in real mode** (exit code 2, with a list of every
problem) without a CUDA GPU, bitsandbytes, all 5 models on disk, or the passcode. It
never falls back to mock. A real server also refuses mock jobs (`provider_mismatch`)
and always uses `configs/run_full_l4.yaml` (uniform 4-bit NF4). Logs:
`/workspace/agentmeter-logs/server.log` and `tunnel.log`. To stop the server and
tunnel: `bash scripts/vast_down.sh`.

### 3. Tunnel: quick or named
| | (a) named tunnel, `CF_TUNNEL_TOKEN=... bash scripts/vast_up.sh` | (b) quick tunnel (default) |
|---|---|---|
| URL | stable, e.g. `https://api.yourdomain.org` | random `https://<words>.trycloudflare.com`, **new on every start** |
| Needs | a domain on Cloudflare (free plan is fine) and a Cloudflare account | nothing |
| Front-end | set `config.json` once | update `config.json` (redeploy) or use `?api=` each session |
| Limits | 100 MB request body on Free/Pro (uploads; URL import is unaffected) | the same, and no uptime guarantee (meant for testing) |

Named tunnel, once: Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create
a tunnel** → *Cloudflared* → name it → copy the **token** from the install command.
Under **Public Hostname**, add e.g. `api.yourdomain.org` → `HTTP` → `localhost:8000`.
On the box:
`export CF_TUNNEL_TOKEN=<token> CF_TUNNEL_HOSTNAME=api.yourdomain.org` before
`vast_up.sh`. Don't put Cloudflare Access (SSO) in front of this hostname: the
page's cross-origin `fetch` calls can't complete an SSO login. The passcode is the
gate.

### 4. Deploy the front-end (static, no functions)
**Vercel:** New Project → import the GitHub repo → **Root Directory `web`** →
Framework preset **Other** → leave Build Command empty, Output Directory `.` → Deploy.
`web/vercel.json` adds no functions, rewrites or proxies.
**Cloudflare Pages:** Create → connect the repo → Framework preset **None** → Build
command *(empty)* → Build output directory **`web`** → Deploy. `web/_headers`
disables caching of `config.json`.

Point it at the backend in **one** of two ways:
- edit `web/config.json` → `{"api_base": "https://api.yourdomain.org"}`, then commit
  and push (an automatic redeploy, no build); or
- open `https://agentmeter.vercel.app/?api=https://<words>.trycloudflare.com` once.
  The browser remembers it; `?api=reset` clears it.

All uploads and downloads go from the browser straight to the backend URL, never
through Vercel (its functions cap request bodies at about 4.5 MB).

### 5. Acceptance test (about 15-30 min including model loads)
1. Open the front-end. The badge must read **Real GPU: <GPU name> · <N> GB**. If it
   reads "GPU backend offline", see Troubleshooting. If it reads "DEMO (mock)", stop:
   you are not talking to the Vast backend.
2. From a terminal:
   `curl -s https://<backend>/health` → `"provider":"real"`, `"gpu":{"name":...}`, and
   5 × `true` under `models_local`.
3. **Prepare:** upload `data/sample_csv/cicids2017_sample.csv` (labelled CIC-IDS2017,
   5 classes, 40 usable rows) with **Flows to benchmark = 20** and Other Attack off.
   Enter the passcode when asked. The prepared set must show *Labelled → accuracy +
   efficiency*, *label aware balanced*, 20 of 40 flows, and the class table at 4
   selected per class.
4. **Benchmark:** choose **Qwen/Qwen2.5-7B-Instruct** and
   **microsoft/Phi-3-mini-4k-instruct** → Run. The line beside Run must say
   *Real GPU*. Wait until 40/40 flows are done; the page can be closed and reopened
   from its job link meanwhile.
5. **Check traceability:**
   - the results page shows the **Real GPU: <name>** badge with the driver and CUDA
     versions;
   - **Download PDF report** has a "Measured on: REAL GPU <name>, <N> GB; NVIDIA driver
     …, CUDA …; provider real (4-bit NF4); host vast.ai instance …" line, and no DEMO
     banner;
   - `curl -s https://<backend>/api/jobs/<job_id>/result | python3 -m json.tool | grep -A12 '"environment"'`
     → `"provider": "real"`, `"gpu_name": ...`, `"driver_version"`, `"cuda_runtime_version"`;
   - the prepared set's `manifest.json` → `prepared_set.backend.provider` = `"real"`.

   Latencies will differ from the Colab A100 and L4 study runs. That is expected: they
   are a property of the GPU, which is why every result names it.
6. Keep the PDF and the prepared set (download them): they are lost when the instance
   is destroyed.

### 6. What lives where, and what is lost
| on the instance's disk (`/workspace`) | lost when the instance is **destroyed** |
|---|---|
| model weights `/workspace/hf` (about 72 GB) | yes: the next instance re-downloads (about 20 min) |
| jobs `results/jobs/`, prepared sets `results/csv_runs|pcap_runs/`, uploads `results/uploads/`, session DBs and PDFs (regenerated on request) | yes: download what you need first |
| logs `/workspace/agentmeter-logs/` | yes |
| the front-end and `config.json` | no: they're on Vercel/Pages |
| the locked study (`agentmeter_full_l4.db`, `data/cicids_full_300.csv`) | not on the box; never written by the service |

**Stopping** a Vast instance keeps its disk, and you keep paying for storage.
**Destroying** it ends billing and deletes the disk.

### 7. Stop billing
1. Download the PDFs/prepared sets you need.
2. Vast console → Instances → **Destroy** (the trash icon), or
   `vastai destroy instance <id>`. Only destroy stops all charges.
3. Optional automatic guard: start with `VAST_API_KEY=... IDLE_MIN=45`. The server
   then destroys its own instance after 45 minutes with no request and no queued or
   running job. Front-end `/health` polling doesn't count as activity, so an open
   browser tab won't keep the GPU alive.
4. Afterwards the front-end shows "GPU backend offline", as intended.

### Troubleshooting
- **Badge "GPU backend offline"**: is the tunnel URL in `config.json` / `?api=` current
  (quick tunnels change on every start)? Does `curl https://<backend>/health` work?
- **Badge online but actions fail with "Cannot reach the GPU backend"**: CORS. Set
  `AGENTMETER_ALLOWED_ORIGINS` to the front-end's exact origin (scheme + host, no path,
  no trailing slash) and restart the server.
- **`vast_models.py` says GATED**: accept the licence on the model page with the
  token's account, wait for approval, re-run.
- **Upload fails at about 100 MB with an HTML 413**: that's Cloudflare's limit. Use
  Import from URL.
- **`refusing to start in REAL mode`**: read the listed problems. Each says what to
  install or set.

## Checklist before going public
- [ ] Served over **HTTPS** (tunnel or reverse proxy), never plain http.
- [ ] `AGENTMETER_TEST_ALLOW_LOOPBACK_URLS` is **not** set.
- [ ] `AGENTMETER_PASSCODE` is a long passphrase; `AGENTMETER_ALLOWED_ORIGINS` lists only your front-end.
- [ ] `/health` says `"provider":"real"` and the badge reads *Real GPU*.
- [ ] `.env` is **not** committed (it's gitignored) and secrets stay in it.
- [ ] You've confirmed a Prepare without the passcode returns **401** (`python -m pytest tests/test_deploy_split.py`).
