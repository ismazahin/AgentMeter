# The IDS gate: the IDS detects, the LLM agents triage

```
flows ──► IDS (Random Forest, CPU) ──► score ≥ τ ? ──► yes: an alert ──► LLM agents: Perceive → Reason → Decide → Act ──► triage verdict
                                             └─────► no: dismissed, no LLM call
```

The IDS is a binary **Random Forest** (attack vs benign) on the CICFlowMeter flow features, the
same features AgentMeter reads. Only the flows it flags reach the LLM agents, so the LLM's job is
**alert triage**: class, MITRE technique, priority, severity, and dismissing the IDS's false
alarms. AgentMeter measures the LLM's resource cost **per alert** and reports, per session:

| | |
|---|---|
| **IDS** | recall, precision, false-positive rate, flagged share, recall per attack class, scoring time per flow |
| **LLM triage of the alerts** (per model) | false alarms dismissed, true attacks it dismissed, the analyst queue |
| **End to end** (per model) | attacks missed (cleared by the IDS + dismissed by the LLM), time / cost / energy per **1,000 incoming flows**, LLM calls saved |
| **τ curve** | the same for every threshold at or above the session's |

Code: `agentmeter/ids/` (model, session stage, analysis), `scripts/train_ids.py`.

## 1. Train the IDS (once, on your computer)

1. **Get the data.** Use the corrected CIC-IDS2017 (Engelen et al., 2021, KU Leuven: the download
   is linked from their paper, `downloads.distrinet-research.be/WTMC2021`), or the original
   CIC-IDS2017 CSVs. Put the CSV files in one folder, for example `data\cicids2017_corrected\`.
2. **Install** (in the repository folder): `pip install -r requirements.txt` (it pins
   `scikit-learn==1.9.1`; the GPU server installs the same version, which loading a model needs).
3. **Train:**
   ```
   python scripts/train_ids.py data\cicids2017_corrected
   ```
   The defaults read up to 400,000 flows per file and fit on up to 500,000; it can take from a few
   minutes to around half an hour depending on the computer. If memory runs out, lower
   `--max-rows-per-file` (e.g. 150000). It prints the threshold and the recall / false-positive
   rate on validation and hold-out.
4. **Result** in `models\ids\`:
   - `ids_rf.joblib`, `ids_rf.json`, `ids_rf.hashes.npy`: the model (not committed to git);
   - `ids_rf_holdout.csv`: **the flows to benchmark with** (the 30% the IDS never saw, up to
     100,000 rows, every class kept). Upload this file in AgentMeter for the study sessions.

What the script does (all of it is written into `ids_rf.json`, for the methodology chapter):
duplicate flows are removed and feature vectors seen with both a benign and an attack label are
dropped; a stratified 70 / 30 split separates training from hold-out; 80% of the training part
fits the forest (100 trees, depth ≤ 20, balanced class weights) and 20% chooses the threshold τ —
the **lowest** τ whose false-positive rate on validation benign flows is ≤ 0.1% (`--max-fpr`),
and never above the τ that keeps validation recall ≥ 99% (`--target-recall`). The IDS is a filter:
it should miss as few attacks as its false-alarm budget allows; the false alarms it lets through
are the LLM triage's work. `"... - Attempted"` flows are excluded. The script prints the hold-out
recall of every attack class (the weakest first).

## 2. Put it on the GPU server

**Once, on the site:** Admin → **IDS model** → upload `ids_rf.joblib`, `ids_rf.json`,
`ids_rf.hashes.npy` → **Upload and use**. Every GPU rented afterwards downloads it at start-up
(`scripts/vast_ids.py`), including GPUs that set themselves up from the Vast template
(`docs/VAST_TEMPLATE.md`). `scripts\vast_start.ps1` still copies `models\ids\ids_rf.*` directly
when the files are on your computer — that copy wins over the stored one. Elsewhere: copy the three files
into `models/ids/` on the server, or set `AGENTMETER_IDS_MODEL=/path/ids_rf.joblib`. Without a
model, the server offers the bundled **demo IDS** (trained on 300 flows; every report says so).

## 3. Use it

New benchmark → Settings → tick **IDS gate** (the IDS's version, threshold and hold-out metrics
are shown; leave the threshold blank to use the model's own). The session's results:
Output Reliability → **IDS → LLM triage**; Overview → the **IDS gate** tile; the PDF report's
"IDS -> LLM triage" section. A session whose flows overlap the IDS's training data is flagged
(use the hold-out CSV to avoid it).

## For the thesis

- Declare: the algorithm and its parameters, the data and its cleaning, the 70/30 split, the τ
  rule (validation recall ≥ 99%), and the hold-out metrics — all in `ids_rf.json`.
- The efficiency results are **per alert triaged**; the per-1,000-incoming-flows figures add the
  IDS's scoring time and scale the LLM's measured time by the flagged share.
- The τ curve covers thresholds at or above the session's only: a lower threshold would send new
  flows to the LLM (a new run).
- PCAP uploads have approximate features (whole-frame lengths): the IDS was trained on CIC
  features, so its scores on PCAP flows are less reliable.
