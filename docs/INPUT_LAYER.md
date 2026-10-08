# Input layer (service pivot): CSV and PCAP

> Next step: run a prepared input through the 4-agent pipeline for up to 2 models.
> See [SESSION_BENCHMARK.md](SESSION_BENCHMARK.md).

The first stage of the benchmarking service turns an upload into a bounded,
representative set of flows plus a metadata record that says what the later LLM
phase may measure. It only ingests and prepares data. It makes **no threat
decisions** and does not touch the 4-agent pipeline, instrumentation, SAW, or the
locked study DB.

## Two input roles

| Upload | Features | Labels | Evaluation mode | Output root |
|---|---|---|---|---|
| **CSV**, CIC-IDS2017 format, labelled | exact (the 78 reference columns) | yes | `accuracy_available`: efficiency + accuracy | `results/csv_runs/<name>/` |
| CSV without a label column | exact | no | `efficiency_only` | `results/csv_runs/<name>/` |
| **PCAP** (raw capture) | approximate (22 of 78 differ, see §2 below) | no | `efficiency_only` | `results/pcap_runs/<name>/` |

```
python scripts/ingest.py data/sample_csv/cicids2017_sample.csv    # type detected from content
python scripts/ingest.py data/sample_pcaps/sample_small.pcap
python scripts/ingest.py Tuesday-WorkingHours.pcap_ISCX.csv --max-flows 300
```

## Unified contract (`agentmeter/ingest/unified.py`)

Both paths write the same three things. The LLM phase reads them with
`load_input_run(dir)` and treats both paths the same way:

| File | Contents |
|---|---|
| `input.json` | `source_type`, `input_role`, `evaluation_mode`, `class_scheme` (5-class / 6-class + class list), `selection_mode` (label_aware_balanced / label_blind_statistical), `capabilities.accuracy`, `accuracy_unavailable_reasons`, `feature_match` (exact / partial / approximate), `feature_columns` (model input), `hidden_columns` (never shown to the model), row counts, rules fired, class mix of the selection |
| `selected_flows.csv` | 7 identification columns + 78 CIC-IDS2017 features + `selection_rule`, `selection_reason`, `matched_rules`. **Never a label column**: this is checked on write and on load |
| `labels.csv` | labelled CSV only: `flow_id, label_raw, label`, held out for scoring and aligned 1:1 with `selected_flows.csv` |

## CSV path (`agentmeter/ingest/csv_input.py`)

Validation rejects a file with a stated reason rather than silently carrying on:
- **Not a CSV:** packet captures (pointed to the PCAP path), binary files, empty
  files, header-only files and unparseable CSVs.
- **Schema:** headers are whitespace-stripped, since the official files use
  `' Label'` and `' Flow Duration'`. All 78 feature columns are required.
  `--allow-partial` accepts a subset, marked `feature_match: partial`, but never
  one that lacks a column the rule-base reads.
- **Values:** rows with NaN/Inf or non-numeric features are dropped and counted, the
  same declared policy as `dataprep.py`. If nothing valid remains, the file is
  rejected.

Labels:
- **Detection:** the label column is found case-insensitively (`Label` / `label`).
- **Mapping:** labels go through `config.yaml data_prep.label_map`, the same
  mapping the study used (for example `SSH-Patator` → Brute Force). Labels that are
  already canonical are accepted.
- **Out-of-taxonomy labels:** rows with labels outside the 5 classes (for example
  `DoS slowloris`) are excluded and counted, because accuracy is only defined over
  the 5 classes.
- **Isolation:** the label is split off before the rule engine runs. The engine is
  label-blind, exactly like the PCAP path, and the class mix of the selection is
  reported afterwards for transparency.

### "Other Attack": 6-class user runs (opt-in)

User CSVs can contain attacks outside the 5 study classes: DoS GoldenEye,
slowloris or Slowhttptest, Heartbleed, Web Attack, Infiltration, Bot.

- **Default:** rows with these labels are excluded and counted, and the run notes how
  to keep them.
- **With `--other-attack`** (`other_attack=True`): they become the 6th class
  **"Other Attack"**. The original label is kept in `labels.csv` as `label_raw`, and
  the report lists how many rows came from each source label.
- **The run's `class_scheme`:** this is the class set that accuracy and confusion must
  be computed over.
  - It is `6-class` only when Other Attack is on *and* at least one row fell into it.
  - A CSV containing only the 5 classes stays `5-class`.
- **Missing labels:** empty or NaN labels are never treated as an attack. Those rows
  are excluded as missing labels.
- **Baseline unchanged:** this applies only to user runs in `results/csv_runs/`. The
  locked baseline (`data/cicids_full_300.csv`, the study DB, `config.yaml classes`)
  stays 5-class and unchanged. A test checks the dataset's SHA-256 fingerprint and
  the DB hashes.

### Selection: label-aware on CSV, label-blind on PCAP

The two paths have different needs, and both go through the same rule engine with an
audit trail:

| Path | Selection mode | Why |
|---|---|---|
| Labelled CSV | `label_aware_balanced`: the `class_balance` constraint caps each class at an equal share of the budget | A CIC-IDS2017 day file is mostly BENIGN. Without the caps, accuracy would mostly reflect one class |
| PCAP / unlabelled CSV / `--label-blind` | `label_blind_statistical`: `class_balance` is skipped and says so in the audit | There are no labels, or you chose not to use them |

`class_balance` reads the held-out labels **for selection only**:
- the labels reach the engine as a separate array and never join the table the rule
  operators read;
- `selected_flows.csv`, the model's input, never contains a label;
- `tests/test_class_balance.py` checks both. It spies on every operator, and it scans
  `selected_flows.csv` for any label value.

Identification columns: the full "TrafficLabelling" CSVs (`Source IP`, `Protocol`,
`Timestamp`…) are mapped onto the identification columns. The MachineLearningCVE
CSVs have no `Protocol` column, so `protocol_name` is `unknown` and protocol
coverage sees one group. This is written into the run's `schema_report.md`.

Per-run extras: `manifest.json` (full validation report), `schema_report.md`
(columns, label column, class table) and `selection_audit.json`.

# PCAP path

## Install (CPU only)

```
pip install -r requirements-pcap.txt
pip install --no-deps cicflowmeter==0.2.0
```

The Python CICFlowMeter port pins `numpy<2`, which would downgrade AgentMeter's
numpy. Its code runs unchanged on numpy 2, so it is installed without its pins.

## Run

```
python scripts/pcap_ingest.py data/sample_pcaps/sample_small.pcap
python scripts/pcap_ingest.py capture.pcapng --max-flows 200 --name lab1
python scripts/pcap_ingest.py --show-rules
```

| Output | Contents |
|---|---|
| `manifest.json` | capture stats, extraction counts, mapping summary, rules fired |
| `flows.csv` | every flow: 7 identification columns + the 78 CIC-IDS2017 features |
| `feature_map.md` / `.json` | per-column mapping to CIC-IDS2017 with status + reason |
| `selected_flows.csv` | the selected subset + `selection_rule`, `selection_reason`, `matched_rules` |
| `selection_audit.json` | each rule's matches/admissions/computed thresholds; why each flow was taken |

## 1. PCAP ingestion (`agentmeter/ingest/pcap.py`)

A file is accepted only if:
1. its magic bytes are libpcap (µs or ns) or pcapng;
2. a libpcap global header reads as version 2.x with a non-zero snaplen; and
3. a full scapy parse succeeds with packets in it.

scapy is lenient: it only *warns* on an unknown link type, a truncated record or a
bad pcapng block, then stops as if at end of file. Those warnings are captured and
reject the file. There is a size limit (default 2 GiB) and an optional `max_packets`
read cap, which is reported in the stats.

### Large inputs on the web service (Phase 43)

The `/service` upload flow applies tighter, configurable bounds (`config.yaml`
`service:`, each with an env override). These only bound the **upload and the parse**.
How flows are sampled and how a benchmark is computed are unchanged.

| limit | default | env override | over the limit |
|---|---|---|---|
| `max_upload_mb` | 200 | `AGENTMETER_MAX_UPLOAD_MB` (or exact `AGENTMETER_MAX_UPLOAD_BYTES`) | **rejected**, HTTP 413 `too_large`, message states the limit |
| `max_pcap_packets` | 100,000 | `AGENTMETER_MAX_PCAP_PACKETS` | parsed up to the cap |
| `max_pcap_flows` | 20,000 | `AGENTMETER_MAX_PCAP_FLOWS` | extraction stops; table = first N flows |
| `max_csv_rows` | 500,000 | `AGENTMETER_MAX_CSV_ROWS` | read stops; selection from first N rows |

- **Upload:** an over-limit `Content-Length` is rejected before any of the body is
  read. Flask's `MAX_CONTENT_LENGTH` (limit + 1 MiB of multipart slack) cuts off a
  body sent without a length (chunked). The file is streamed to disk in 1 MiB chunks
  with a running byte count, so it is never held whole in memory. The page also
  checks `file.size` first, so a browser never sends an oversized file.
- **Caps:** when one applies, the run summary's `large_input` says so
  (`{"capped": true, "unit": "rows"|"flows"|"packets", "cap": N, "message": …}`),
  the UI shows it as a notice, and the CSV `schema_report.md` gets a "Large input" line.
  The rule-base then selects its bounded set (`max_flows` ≤ 500) from what was parsed.
- **Why these defaults:** PCAP parsing is the slow step, at about 0.6 ms per packet
  on CPU (validation pass plus CICFlowMeter). The packet/flow caps keep a worst-case
  ingest near a minute. A 520k-row (166 MB) CSV ingests in about 17 s at the
  500k-row cap.
- The CLI (`scripts/ingest.py`) keeps its own options (`--max-packets`, `--max-rows`)
  and the 2 GiB file bound. Phase 43 does not change it.

## 2. PCAP flow extraction (`flows.py`, `feature_map.py`)

- **Backend:** the Python port `cicflowmeter==0.2.0`. Its CLI reads files through a
  sniffer that needs the `tcpdump` binary, so instead packets are streamed with scapy
  and fed to the port's own `FlowSession`. Flow assembly and feature maths are the
  port's own; only the I/O is ours.
- **Scope:** the port builds flows from IPv4 TCP/UDP only. ICMP, ARP and IPv6 packets
  are counted as skipped.
- **Mapping:** all 78 CIC-IDS2017 columns, in the dataset's order, are produced and
  mapped explicitly. The status of each was set from the port's source code:

| Status | Count | Meaning |
|---|---|---|
| direct | 50 | same definition and units (durations/IATs in µs) |
| duplicate | 1 | `Fwd Header Length.1`, a duplicate in CIC-IDS2017 itself |
| approximate | 5 | the port copies a related value: `CWE Flag Count` (copy of Fwd URG Flags) and the four `Subflow *` columns (copies of totals) |
| semantic_diff | 22 | the port measures **whole-frame** length where CIC-IDS2017 used **payload** bytes (all packet-length, byte-total, byte-rate and segment-size columns), and its header-length columns count IP-header bytes |

**Consequence:** the 22 `semantic_diff` columns are internally consistent across
uploaded PCAPs but are **not numerically comparable** with the CIC-IDS2017 CSV rows.
If later phases need parity on these, the remedy is a payload-length backend (or the
Java CICFlowMeter) behind the same `extract_flows` interface. The gap is reported in
every run's `feature_map.md`, not hidden.

## 3. Rule-based selection (both paths) (`rules.py`, `configs/flow_rules.yaml`)

An inspectable rule engine. The rule-base is YAML; each rule is an instance of one of
five operator types:

| Type | Role | Example in the default rule-base |
|---|---|---|
| `per_group` | coverage | up to 25 flows per protocol; 3 per busiest destination port |
| `top_percentile` | notable (statistical) | packets / duration / bytes ≥ p95 of this capture (and above the median) |
| `threshold` | notable (absolute) | `Flow Duration >= 60 s` |
| `typical_band` | baseline for balance | flows inside p25–p75 on packets, duration and bytes |
| `random_fill` | fill | seeded random sample of any budget left |
| `class_balance` | **constraint** (labelled CSV only) | water-filling caps: an equal share of the budget per label class, and a small class keeps all its rows while its spare share is re-split. Admits nothing itself, and is position-independent. The rules above still choose *which* rows within each class |

How the engine decides:
- **Order and budget:** rules run in declared order. `budget.max_flows` caps the
  total; `quota` caps one rule.
- **Reserve:** `reserve` holds slots back from earlier rules. A rule can always use
  its own reserve.
- **One admission per flow:** a flow is admitted once, by the first rule that takes
  it. Every other rule it matched is still recorded, so the audit gives all reasons.
- **Budget override:** `--max-flows` scales quotas and reserves proportionally, so
  the rule mix holds at any budget.
- **Reproducible:** selection is deterministic for a given `seed`.

"Notable" means statistically unusual **in this capture**. No rule judges whether
traffic is malicious.

Adding or re-tuning a rule is a YAML edit (`tests/test_flow_rules.py` proves this).
Only a new operator *type* needs code: one function registered in `RULE_TYPES`.

## Tests

- `tests/test_class_balance.py` needs pandas only. It covers:
  - water-filling
  - an imbalanced CSV: label-blind selection is BENIGN-dominated, while balanced gives
    10 rows per class
  - the YAML toggle and config errors
  - label isolation, using operator spies and a scan of the output
  - Other Attack: the 6-class scheme and its distribution, 5-class by default, and
    missing labels never counted as attacks
  - the locked dataset fingerprint, DB hashes and the 5 study classes
- `tests/test_csv_ingest.py` needs pandas only. It covers:
  - the CSV sample, which must match its generator
  - schema, label and class-distribution reporting, and raw-to-canonical label mapping
  - label isolation (the rule engine is spied on to confirm it never sees a label)
  - accuracy-available, efficiency-only and partial runs
  - every rejection path, and the contract's leak and alignment guards
  - the unified CLI

- `tests/test_flow_rules.py` needs pandas only. It covers the feature list (pinned to
  `data/cicids_full_300.csv`), the mapping report, and the rule semantics: budget,
  quota, reserve, scaling, determinism, disabled rules, config errors and adding a
  rule by YAML.
- `tests/test_pcap_ingest.py` needs scapy and cicflowmeter. It covers:
  - the generated sample capture, which must match the committed
    `data/sample_pcaps/sample_small.pcap` byte for byte
  - validation of pcap and pcapng, and rejection of bad files
  - extraction values and units
  - end-to-end output files
  - the study DBs, unchanged
  - an import guard: `ingest/` never imports the pipeline, analysis, DB or run modules
