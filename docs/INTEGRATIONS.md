# External services AgentMeter calls

AgentMeter **provides** three APIs, described endpoint by endpoint in the OpenAPI documents
(`web/docs/openapi-*.json`, shown at `/docs/` on the site; built by `scripts/build_openapi.py`).
This page is about the other direction: the third-party services AgentMeter **calls**, what it
sends them, which credential it uses and what happens when they fail. None of them is needed to
read results that are already stored.

| Service | What AgentMeter calls | Why | Credential (where it lives) | Data sent | If it fails |
|---|---|---|---|---|---|
| **Hugging Face Hub** | `GET https://huggingface.co/api/models/{id}` (model metadata, `?blobs=true` for file sizes); model files through `huggingface_hub` (`snapshot_download`, `hf_hub_download`) | Check that a model exists, is an original (not pre-quantised) open model and how large it is (Admin › Model catalog); show licence and size as context; download weights before a benchmark | `HF_TOKEN` (GPU backend's `.env`; only gated models need it) | The model id; nothing about users or flows | Adding a model is refused with the reason; a benchmark whose model cannot be downloaded fails with a clear error before any flow runs; metadata shows "unknown" |
| **Telegram Bot API** | `POST https://api.telegram.org/bot<token>/sendMessage` | "Notify me" when a run finishes or fails, and the "a result moved" alert | `TELEGRAM_BOT_TOKEN` (control-plane secret; one-user mode: the backend's `.env`) | The chat id the user saved, the session's models, headline numbers and a link | The notice is skipped (logged); a run or a result is never affected |
| **Vast.ai** | `GET https://console.vast.ai/api/v0/instances/{id}/` (the hourly price) and `DELETE …/instances/{id}/` (destroy) | Cost per 1,000 flows from the real rental price; the cost guard destroys an idle rented GPU | `VAST_API_KEY`, `VAST_INSTANCE_ID` (GPU backend's `.env`) | The instance id | No price: cost shows "not measured". A failed destroy is logged and the instance keeps running (and costing money) — check the Vast.ai console |
| **Supabase** (platform) | Postgres (the control plane's tables), Storage (signed upload / download URLs), the Edge runtime | Accounts, stored sessions and files; large files go straight to Storage | The function's own database role and Storage key (Supabase secrets; never in the browser) | Everything the control plane stores | The site says "Cannot reach the control plane"; the GPU backend keeps running and re-sends its sessions later |
| **Cloud preparation service** (AgentMeter's own code, hosted on Cloud Run or a Space) | `POST {prep}/prepare`, `GET {prep}/health` (see its OpenAPI document) | Prepare data while the GPU is busy | `CLOUD_PREP_SECRET` (shared: Supabase secret + the service's environment) | The uploaded file (via a single-use URL), the preparation options | New uploads are prepared in the browser instead (or on the GPU server once it is free) |
| **jsDelivr** (CDN) | Pyodide 0.28.3 files | Preparing data in the browser | none | nothing (static downloads) | Browser preparation is unavailable; the GPU server or the cloud prepares instead |
| **Google Fonts** | Font files | The site's typeface | none | nothing | The page uses system fonts |

## Dependency risks and fallbacks (for the thesis)

- **A model is renamed or removed on Hugging Face.** Stored sessions are unaffected (their numbers
  are kept); new benchmarks of that model fail at the download step with the reason. The model
  catalog's verification flags it.
- **Telegram rate-limits or is down.** Alerts are best effort; nothing waits for them.
- **Vast.ai changes its API.** The price lookup returns nothing (cost "not measured") and the
  cost guard cannot destroy the instance: the backend logs it (Admin › Problems in the last 24
  hours); destroy the instance by hand.
- **Supabase is unreachable.** Logging in and stored results are unavailable until it returns; the
  GPU backend keeps its own copy of each session and re-sends it.
- **The cloud preparation service is cold or down.** Uploads are prepared in the browser (the same
  Python code, the same flows and hash) or wait for the GPU server.

## Rotating the credentials

See `docs/SECURITY.md` (one table: where each secret lives and how to replace it).
