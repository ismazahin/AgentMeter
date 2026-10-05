# PCAP input layer (service pivot, stage 1)

First stage of the benchmarking service: **PCAP → flows → rule-based selection of
representative flows**. It only extracts and samples flows. It makes **no threat
decisions** and does not touch the 4-agent pipeline, instrumentation, SAW, or the
locked study DB. Outputs go to `results/pcap_runs/<name>/` only.

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

## 1. Ingestion (`agentmeter/ingest/pcap.py`)

A file is accepted only if:
1. its magic bytes are libpcap (µs or ns) or pcapng;
2. a libpcap global header reads as version 2.x with a non-zero snaplen; and
3. a full scapy parse succeeds with packets in it.

scapy is lenient: it only *warns* on an unknown link type, a truncated record or a
bad pcapng block, then stops as if at end of file. Those warnings are captured and
reject the file. There is a size limit (default 2 GiB) and an optional `max_packets`
read cap, which is reported in the stats.

## 2. Flow extraction (`flows.py`, `feature_map.py`)

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

## 3. Rule-based selection (`rules.py`, `configs/flow_rules.yaml`)

An inspectable rule engine. The rule-base is YAML; each rule is an instance of one of
five operator types:

| Type | Role | Example in the default rule-base |
|---|---|---|
| `per_group` | coverage | up to 25 flows per protocol; 3 per busiest destination port |
| `top_percentile` | notable (statistical) | packets / duration / bytes ≥ p95 of this capture (and above the median) |
| `threshold` | notable (absolute) | `Flow Duration >= 60 s` |
| `typical_band` | baseline for balance | flows inside p25–p75 on packets, duration and bytes |
| `random_fill` | fill | seeded random sample of any budget left |

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
