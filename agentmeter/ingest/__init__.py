"""PCAP input layer for the AgentMeter benchmarking service (INPUT ONLY).

    PCAP file -> validate + stats (pcap.py)
              -> per-flow CIC-IDS2017-aligned features via CICFlowMeter (flows.py)
              -> explicit rule-based selection of a bounded subset (rules.py)
              -> results/pcap_runs/<name>/ (run.py)

This layer only extracts and samples flows. It makes NO threat decisions and does
not touch the 4-agent pipeline, instrumentation, SAW, or the locked study DB.

Not re-exported from `agentmeter/__init__.py` on purpose: scapy/cicflowmeter are
optional dependencies (requirements-pcap.txt), imported lazily here so the rest of
the package keeps working without them.
"""
