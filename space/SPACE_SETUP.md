# Deploying the AgentMeter pilot to a Hugging Face Space (1× L4)

Prepare-only checklist. Nothing here has been run on paid hardware — you run it.

## 0. Before you start (no cost yet)

- [ ] Hugging Face account + a **read** access token (Settings → Access Tokens).
- [ ] Accept the gated licence for the pilot model on its Hub page:
      `mistralai/Mistral-7B-Instruct-v0.3` (and `meta-llama/Meta-Llama-3-8B-Instruct`
      if you'll compare later).
- [ ] Never paste the token into code or commit it — it goes in a Space **secret**.

## 1. Create the Space

1. huggingface.co → **New Space**.
2. SDK: **Gradio**. Name it e.g. `agentmeter-pilot`. Visibility: your choice.
3. Create it (starts on free CPU — still no GPU cost yet).

## 2. Add the files (all at the Space repo ROOT)

Copy from this repo into the Space:

| From (this repo) | To (Space root) |
|---|---|
| `space/app.py` | `app.py` |
| `space/requirements.txt` | `requirements.txt` |
| `space/README.md` | `README.md` |
| `agentmeter/` (whole folder) | `agentmeter/` |
| `configs/` (whole folder) | `configs/` |
| `data/` (whole folder) | `data/` |
| `main.py` | `main.py` |

Easiest path: clone the Space repo, copy those in, `git add . && git commit && git push`.
(Do **not** copy this repo's root `README.md` or root `requirements.txt` — use the
`space/` versions so the Space YAML header and GPU deps are correct.)

## 3. Add the token secret

Space → **Settings → Variables and secrets → New secret**:
`HF_TOKEN` = your read token.

## 4. 🟢 Switch to GPU  — THIS STARTS BILLING

- Space → **Settings → Hardware → Nvidia L4 (small)** (~$0.80/hr).
- The Space rebuilds. When the app loads, both banners should be green
  (GPU detected + HF_TOKEN set).

## 5. Run the pilot

- Click **Run pilot** (default: 8 scenarios). First run also downloads the
  model weights (~15 GB) — that is one-time and excluded from per-agent timings.
- When it finishes you'll see the per-agent table, projection, and accuracy.
- Click **Download pilot.json** and send me that file (or paste the report text).

## 6. 🔴 PAUSE THE SPACE  — STOP BILLING

- Space → **Settings → Pause** immediately after the run.
- A paused Space is not billed. Do this even if you plan to run again later.

## Alternative: run from the Space terminal

If you prefer the CLI to the UI, in the Space (GPU on) open a terminal:

```bash
python main.py --config configs/pilot_mistral_l4.yaml pilot
```

Same output; still remember to **pause** afterwards.

## Rough cost sanity check

The L4 is ~$0.80/hr ≈ RM3.8/hr. A pilot (model download + 8 scenarios) should be
well under an hour. Keep an eye on the clock and pause promptly; the projection
the pilot prints will tell you what a full precompute run would cost before you
commit to it.
