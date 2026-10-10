# Reproducing the locked 5-model L4 baseline (command line only)

The validated study, Chapter 4's baseline, is five models (Mistral-7B-Instruct-v0.3,
Meta-Llama-3-8B-Instruct, Qwen2.5-7B-Instruct, Phi-3-mini-4k-instruct, gemma-2-9b-it)
× 300 balanced CIC-IDS2017 scenarios. It was run on an **NVIDIA L4**, uniform 4-bit NF4,
one model at a time. Its artefacts are **locked**:

| artefact | path | status |
|---|---|---|
| run config | `configs/run_full_l4.yaml` | never edited |
| dataset | `data/cicids_full_300.csv` (sha256 `ef1b13787e2c8760…`) | never rewritten |
| results DB | `results/agentmeter_full_l4.db` | read-only (not in git) |

Reproduction uses **only the CLI** (`main.py`). No web page triggers it; the benchmark
app (`/`, New benchmark) is for your own data, and the baseline is not in its navigation. AgentMeter measures LLM efficiency; it is
not a threat-detection product.

## 0. Machine
- An NVIDIA **L4** (24 GB). Latency and VRAM are only comparable on the same GPU model.
  The Colab notebook refuses to run on anything else. Disk: about 100 GB for the five
  models' weights.
- Python 3.11, then:
  ```bash
  pip install -r requirements.txt -r requirements-gpu.txt
  export HF_TOKEN=hf_...   # Llama-3 and gemma-2 are gated: accept their licences first
  python main.py check-env
  ```

## 1. Dataset (only if you rebuild it from the official CIC-IDS2017 CSVs)
```bash
# put the official MachineLearningCVE CSVs in data/cicids_raw/  (config.yaml data_prep)
python main.py build-dataset --output results/repro_cicids_300.csv
sha256sum results/repro_cicids_300.csv data/cicids_full_300.csv    # must match ef1b1378…
```
`build-dataset` is seeded (seed 42, 60 per class), so the hashes must match. Never
write over `data/cicids_full_300.csv`; the study uses that file as committed.

## 2. Full run, into a SEPARATE database
`configs/run_full_l4.yaml` points at the locked DB. If that DB is on the machine,
`run-full` would resume or append to it. For a reproduction, copy the config with a
new DB path; the locked config itself stays unedited:
```bash
sed 's#results/agentmeter_full_l4.db#results/repro_full_l4.db#' configs/run_full_l4.yaml > results/repro_full_l4.yaml
python main.py --config results/repro_full_l4.yaml run-full --no-analyze
```
The run is sequential (one worker subprocess per model) and resumable: re-run the same
command after a disconnect. Expect several GPU-hours on an L4. On Colab, use
`notebooks/run_full_l4_colab.ipynb` (with the same config change). On a Linux box,
`./run_full.sh` wraps the same command for the locked config.

## 3. VRAM footprint (load-and-read only, no scenarios)
```bash
python main.py --config results/repro_full_l4.yaml measure-vram --out results/repro_model_vram.json
```
On Colab: `notebooks/measure_vram_l4_colab.ipynb`.

## 4. Analysis (accuracy, SAW composite + tiers, Kruskal-Wallis / Dunn, per-class, advanced)
```bash
python main.py --config results/repro_full_l4.yaml analyze --db results/repro_full_l4.db \
    --model-vram results/repro_model_vram.json --out results/repro_analysis
```
This reproduction's `analysis.json` is labelled `"run_kind": "user_run"` /
`non_validated`, because only the locked DB is the validated baseline. Compare its
ranking, composites, tiers and statistics with the locked analysis:
```bash
python main.py --config configs/run_full_l4.yaml analyze --out results/analysis   # read-only on the locked DB
```

## 5. View it
Open the Validation-baseline page: `python scripts/serve.py`, then `http://localhost:8000/baseline`
(direct URL only; it is not in the app's navigation).
Or use it without a server: `python -m http.server -d dashboard 8080`, then
<http://localhost:8080/>, then **Load results.json**. Copying an `analysis.json` to
`dashboard/analysis.json` makes it auto-load.

## Archived command-line tools (kept, not in any UI)
| tool | purpose |
|---|---|
| `python main.py pilot` | Phase 5 pilot (1 model, a few scenarios); `notebooks/agentmeter_pilot_colab.ipynb`, `configs/pilot_*.yaml`, `configs/smoke_gpt2.yaml` |
| `python scripts/report.py results/pilot.json` | HTML report of a pilot run |
| `python scripts/compare_app.py` | Gradio side-by-side of pilot JSONs (`requirements-demo.txt`) |
| `space/` | the Phase 5 pilot as a Hugging Face Space (historical) |
| `python scripts/demo_run.py` | stream ONE scenario through the real pipeline (live demo) |
| `python main.py check-integrity` | read-only cross-check of the study DB and the app DB |
| `python main.py combine-db --study … --app … --out …` | merge the legacy study + app DBs into a new file (sources unchanged) |
| `python main.py load-data / run-pipeline / bench` | developer harness (mock) |
