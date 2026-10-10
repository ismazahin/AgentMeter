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

### Large inputs on the web service (Phases 43 / 43b)

The New-benchmark wizard has two stages under the hood. **Prepare** (data preparation for benchmarking, not
analysis) turns a raw file into a small prepared set. **Benchmark** reads only that
set. Prepare runs as a background job on the Phase 40 job layer (status, resume, the
same single-job lock as benchmarks), so the upload request only saves the file. Limits
are set in `config.yaml` `service:`, and each has an env override:

| limit | default | over the limit |
|---|---|---|
| `max_upload_mb` | 90 (below Cloudflare's 100 MB) | rejected, HTTP 413, before the body is read; the page points to URL import |
| `max_url_download_gb` | 10 | refused from Content-Length, or stopped mid-stream; the partial file is deleted |
| `url_timeout_s` | 3600 | the download is stopped |
| `max_csv_rows` | 500,000 | the CSV candidate pool is a stratified sample of the **whole** file |
| `max_pcap_packets` / `pcap_windows` | 100,000 / 10 | the capture is sampled as 10 evenly spaced **time windows** |
| `max_pcap_flows` | 20,000 | the PCAP pool is a seeded **uniform** subsample of the extracted flows |

**Sampling never takes the first N rows or packets:**
- **CSV** (`csv_input.read_pool`) makes one streaming pass in 100k-row chunks. Each row
  gets a seeded uniform random key. Per stratum (the raw label, or one stratum when
  unlabelled), the rows with the smallest keys are kept, up to a water-filled share
  of the pool: rare labels keep every row, the rest share the remainder equally. That
  is a uniform sample within each label, drawn from the whole file in bounded memory,
  and no label present in the file is lost from the pool. Pooled rows keep their file
  position, so `flow_id` is still the data-row number. A file within the cap is read
  in full and is byte-for-byte what it was before.
- **PCAP** (`pcap_window.py`): a header-only pass (no decoding) counts packets, finds
  the time span and rejects a structurally broken file. A capture within
  `max_pcap_packets` is parsed in full, unchanged. Otherwise K windows start at
  `first_ts + i·span/K`, each taking the next `max_packets/K` packets. Those records
  are copied verbatim (libpcap or pcapng, all non-packet pcapng blocks kept) into a
  smaller capture that the existing validation and CICFlowMeter read. Flows crossing a
  window edge are cut there. Every window (start, packets taken) is recorded.
- The flow-selection rules (`flow_rules.yaml`, including `class_balance`) then run on
  the pool, unchanged.

**Reporting.** The summary, the UI notice, `manifest.json` (`prepared_set.sampling`,
`class_counts` with `in_file` / `in_pool` / `selected` and the classes lost at each
stage) and the PDF all state the method, the pool size or windows, and any class
absent from the prepared set.

**Prepared set** (`server/prepare.py`): the run directory plus `features.csv`
(identification columns, 78 features and selection provenance; no labels),
`labels.csv` (labelled CSV only) and `manifest.json` (source filename or URL, size,
sha256, input type, limits, sampling, every rule fired and flows admitted, class
counts, timestamps, tool versions and the `input.json` record). It is downloadable
and can be re-uploaded on the Benchmark page. The import checks the manifest
schema, the file hashes, the column contract, label isolation and alignment.

Measured on CPU (URL import from a local https server, default limits): a 384 MB,
1.2M-row CSV prepared in 22.5 s (pool 500k, peak memory about 1.6 GB). A 172 MB,
2M-packet capture prepared in 59 s (10 windows, 100k packets parsed, about 45 s of
that in CICFlowMeter).

The CLI (`scripts/ingest.py`) keeps its own options and the 2 GiB file bound.

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
