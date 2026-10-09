# Deploying AgentMeter (internet access, safely)

The dashboard + API is a Flask app (`scripts/pull_eval_server.py`). You can make it
reachable over the internet, but **read the security section first** — some endpoints
(e.g. `/pull-eval`) spend GPU money, so it must not be open to strangers.

## Security model (do this before any public exposure)

The server has an **opt-in HTTP Basic Auth gate**. It is OFF for local use and ON as
soon as you set a password in `.env`:

```
AGENTMETER_AUTH_USER=agentmeter      # pick any username
AGENTMETER_AUTH_PASS=<a long random password>
```

When set, **every** route (dashboard + API) requires those credentials. The browser
prompts once and reuses them for the page's `fetch` calls too. Only `/health` and
CORS preflight (`OPTIONS`) stay open.

- Basic Auth sends the password in each request, so **always serve over HTTPS** — a
  tunnel or reverse proxy (below) provides the TLS. Don't expose plain `http://`.
- Restart the server after changing these values.
- If you open a public tunnel (`--ngrok`) without a password set, the server prints a
  loud warning — heed it.

## The GPU stays on the GPU box

Exposing the dashboard does not move the benchmark. Runs still need an NVIDIA GPU +
the model environment. Typical layout:

- **Dashboard/server** on a small always-on machine (or your laptop), reachable to you.
- **Runs** on the GPU box (your rented Vast.ai instance), triggered on that box
  (CLI / the downloadable runner / the remote runner in the Config builder).
- New analyses copied back into `results/` are picked up by Local results, and (if
  configured) pushed to your phone via Telegram (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`).

## Ways to expose it (easiest + safest first)

### 1. Tailscale — recommended for personal / thesis use
A private network between *your* devices only. No open ports, no public URL, nothing
for strangers to find.
1. Install Tailscale on the server machine and on your phone/laptop; sign in to the
   same account.
2. Run the server normally: `python scripts/pull_eval_server.py`.
3. From your phone/laptop open `http://<tailscale-ip-of-server>:8000/`.
Auth is optional here (only your devices can reach it), but setting it is still good hygiene.

### 2. Cloudflare Tunnel (public URL, with a login)
Gives a stable `https://…` URL and can put Cloudflare Access (SSO/one-time-PIN) in
front. Install `cloudflared`, then `cloudflared tunnel --url http://localhost:8000`.
Set the Basic Auth password too, as defence in depth.

### 3. ngrok — quick, supervised demo only
Built in: `python scripts/pull_eval_server.py --ngrok` prints a public `https://…`
URL. **Set `AGENTMETER_AUTH_PASS` first** — the ngrok URL is not secret. Good for a
short live demo you watch; don't leave it running unattended.

### 4. Reverse proxy + production server (proper hosting)
For a long-lived public deployment:
- Put **Caddy** or **nginx** in front for HTTPS (Caddy gets certificates automatically).
- Keep `AGENTMETER_AUTH_PASS` set (or use the proxy's own auth).
- Optionally run under a production WSGI server instead of Flask's dev server:
  - Windows: `pip install waitress` then serve the app object.
  - Linux: `pip install gunicorn` similarly.
  For a single user, the built-in threaded dev server behind a tunnel is acceptable;
  the real security win is **auth + HTTPS**, which the steps above give you.

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
- [ ] `AGENTMETER_AUTH_USER` / `AGENTMETER_AUTH_PASS` set to a strong password.
- [ ] Served over **HTTPS** (tunnel or reverse proxy), never plain http.
- [ ] `AGENTMETER_TEST_ALLOW_LOOPBACK_URLS` is **not** set.
- [ ] `AGENTMETER_PASSCODE` is a long passphrase; `AGENTMETER_ALLOWED_ORIGINS` lists only your front-end.
- [ ] `/health` says `"provider":"real"` and the badge reads *Real GPU*.
- [ ] `.env` is **not** committed (it's gitignored) and secrets stay in it.
- [ ] You've confirmed `/pull-eval` returns **401** without credentials
      (`python -m pytest tests/test_auth.py`).
- [ ] Telegram keys set if you want phone alerts; SSH/Vast keys set if you use the
      remote runner.
