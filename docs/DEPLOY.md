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

## Checklist before going public
- [ ] `AGENTMETER_AUTH_USER` / `AGENTMETER_AUTH_PASS` set to a strong password.
- [ ] Served over **HTTPS** (tunnel or reverse proxy), never plain http.
- [ ] `.env` is **not** committed (it's gitignored) and secrets stay in it.
- [ ] You've confirmed `/pull-eval` returns **401** without credentials
      (`python -m pytest tests/test_auth.py`).
- [ ] Telegram keys set if you want phone alerts; SSH/Vast keys set if you use the
      remote runner.
