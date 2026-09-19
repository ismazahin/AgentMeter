"""Generate dashboard/sample_analysis.json.

The SAW numbers (accuracy, composite, rank, tier, VRAM->1.0) are the LOCKED real
results. latency/token normalised values are SOLVED so that under DEFAULT weights
the composite reproduces the locked composites EXACTLY; their raw magnitudes and all
per-agent numbers are ILLUSTRATIVE placeholders until a real analysis.json is loaded.
"""
import json
from pathlib import Path

WEIGHTS = {"accuracy": 0.40, "latency": 0.25, "vram": 0.20, "tokens": 0.15}
TARGETS = {"accuracy_pct": 80.0, "latency_s": 5.0, "vram_mb": 16000.0, "tokens_total": 1200.0}
TIERS = {"healthy_min": 80.0, "degraded_min": 60.0}
WEIGHT_SETS = {
    "default": dict(WEIGHTS),
    "equal": {"accuracy": 0.25, "latency": 0.25, "vram": 0.25, "tokens": 0.25},
    "accuracy_heavy": {"accuracy": 0.55, "latency": 0.20, "vram": 0.15, "tokens": 0.10},
    "efficiency_heavy": {"accuracy": 0.25, "latency": 0.30, "vram": 0.30, "tokens": 0.15},
}

# model, accuracy(fraction), locked composite, illustrative total device VRAM (MB)
MODELS = [
    ("Qwen2.5-7B-Instruct",       0.267, 0.489, 5200.0),
    ("Mistral-7B-Instruct-v0.3",  0.333, 0.462, 5000.0),
    ("Meta-Llama-3-8B-Instruct",  0.183, 0.418, 5600.0),
    ("gemma-2-9b-it",             0.260, 0.416, 6600.0),
    ("Phi-3-mini-4k-instruct",    0.207, 0.411, 3100.0),
]


def clamp(x):
    return float(min(1.0, max(0.0, x)))


def composite(norm, weights):
    s = sum(weights.values())
    w = {k: weights[k] / s for k in weights}
    return sum(norm[k] * w[k] for k in ("accuracy", "latency", "vram", "tokens"))


def tier(score100):
    if score100 >= TIERS["healthy_min"]:
        return "Healthy"
    if score100 >= TIERS["degraded_min"]:
        return "Degraded"
    return "Critical"


raw_rows, norm_rows = [], []
for name, acc_frac, comp_locked, total_vram in MODELS:
    acc_pct = acc_frac * 100.0
    acc_norm = clamp(acc_pct / TARGETS["accuracy_pct"])
    vram_norm = clamp(TARGETS["vram_mb"] / total_vram)   # 1.0 (below target)
    # solve latency_norm == tokens_norm == v so default composite == locked:
    #   0.40*acc_norm + 0.20*vram_norm + (0.25+0.15)*v = comp_locked
    v = (comp_locked - WEIGHTS["accuracy"] * acc_norm - WEIGHTS["vram"] * vram_norm) / \
        (WEIGHTS["latency"] + WEIGHTS["tokens"])
    lat_norm = tok_norm = v
    # back out ILLUSTRATIVE raw magnitudes consistent with target/value normalisation
    latency_s = TARGETS["latency_s"] / v
    tokens_total = TARGETS["tokens_total"] / v
    working = round(total_vram * 0.25, 1)          # illustrative marginal split
    weight_fp = round(total_vram - working, 1)
    raw_rows.append({
        "model": name,
        "accuracy_pct": round(acc_pct, 4),
        "latency_s": round(latency_s, 4),
        "vram_working_mb": working,
        "tokens_total": round(tokens_total, 2),
        "weight_footprint_mb": weight_fp,
        "total_device_vram_mb": total_vram,
        "vram_mb": total_vram,
        "vram_source": "total_device_footprint",
    })
    norm_rows.append({"model": name, "accuracy": acc_norm, "latency": lat_norm,
                      "vram": vram_norm, "tokens": tok_norm})

# saw_table = raw merged with normalised + composite/rank/tier (sorted desc)
merged = []
for raw, nm in zip(raw_rows, norm_rows):
    row = dict(raw)
    row.update({"accuracy": nm["accuracy"], "latency": nm["latency"],
                "vram": nm["vram"], "tokens": nm["tokens"]})
    row["composite"] = composite(nm, WEIGHTS)
    merged.append(row)
merged.sort(key=lambda r: r["composite"], reverse=True)
for i, row in enumerate(merged):
    row["rank"] = i + 1
    row["tier"] = tier(row["composite"] * 100.0)

# sanity: composites must match the locked values
locked = {m[0]: m[2] for m in MODELS}
for row in merged:
    assert abs(row["composite"] - locked[row["model"]]) < 1e-9, \
        (row["model"], row["composite"], locked[row["model"]])

# sensitivity ranks/scores across all weight sets
sens_ranks = {"model": [r["model"] for r in norm_rows]}
sens_scores = {"model": [r["model"] for r in norm_rows]}
top_by_set = {}
# build rank/score columns
for set_name, w in WEIGHT_SETS.items():
    scored = [(nm["model"], composite(nm, w)) for nm in norm_rows]
    order = sorted(scored, key=lambda x: x[1], reverse=True)
    rank_of = {m: i + 1 for i, (m, _) in enumerate(order)}
    sens_ranks[set_name] = [rank_of[nm["model"]] for nm in norm_rows]
    sens_scores[set_name] = [round(composite(nm, w), 6) for nm in norm_rows]
    top_by_set[set_name] = order[0][0]
tops = set(top_by_set.values())
top_stable = len(tops) == 1

# rows-of-records form (as pandas to_dict("records"))
def records(colmap):
    n = len(colmap["model"])
    return [{k: colmap[k][i] for k in colmap} for i in range(n)]

# per-agent diagnostics: reason dominates wall_time; decide dominates vram_delta
AGENTS = ["perceive", "reason", "decide", "act"]
per_agent_table = []
per_agent_dominant = []
for name, acc_frac, comp_locked, total_vram in MODELS:
    base = 0.9 + 0.05 * (total_vram / 5000.0)
    profile = {
        "perceive": {"wall": base * 0.5, "vram": 40.0},
        "reason":   {"wall": base * 3.2, "vram": 120.0},   # dominates wall_time
        "decide":   {"wall": base * 1.1, "vram": 380.0},   # dominates vram_delta
        "act":      {"wall": base * 0.6, "vram": 60.0},
    }
    for ag in AGENTS:
        p = profile[ag]
        per_agent_table.append({
            "model": name,
            "agent_name": ag,
            "mean_wall_s": round(p["wall"], 4),
            "mean_ttft_s": round(p["wall"] * 0.35, 4),
            "mean_vram_delta_mb": round(p["vram"], 2),
            "mean_input_tokens": round(180.0 if ag != "reason" else 260.0, 1),
            "mean_output_tokens": round(40.0 if ag != "reason" else 120.0, 1),
        })
    per_agent_dominant.append({
        "model": name,
        "latency_dominant_agent": "reason",
        "latency_dominant_mean_wall_s": round(profile["reason"]["wall"], 4),
        "vram_dominant_agent": "decide",
        "vram_dominant_mean_vram_delta_mb": round(profile["decide"]["vram"], 2),
    })

finding = (
    "VRAM (total device footprint) ranges 3.1-6.6 GB across models, all well below "
    "the 16 GB target -> VRAM is not a binding constraint on this hardware; it "
    "normalises to 1.0 for all models and does not differentiate the ranking. "
    "Differentiation is driven by accuracy, latency, and tokens."
)

payload = {
    "run_ids": ["SAMPLE-illustrative"],
    "notes": {
        "SAMPLE": ("ILLUSTRATIVE sample. accuracy / composite / rank / tier and the "
                   "VRAM->1.0 finding are the LOCKED real L4 results. latency & token "
                   "MAGNITUDES and ALL per-agent numbers are placeholders solved to "
                   "reproduce the locked composites under default weights; load a real "
                   "analysis.json to replace them."),
        "tokens_definition": ("tokens = mean over scenarios of the per-scenario TOTAL "
                              "tokens (sum over the 4 agents of input+output)."),
        "normalisation_formula": ("benefit (accuracy): norm = clamp(value/target,0,1); "
                                  "cost (latency,vram,tokens): norm = clamp(target/value,0,1)"),
        "vram_per_agent_note": ("vram_delta (per-agent) is MARGINAL working memory above "
                                "the fresh-context baseline and EXCLUDES model weights."),
        "vram_saw_criterion": ("SAW VRAM criterion = TOTAL device footprint = "
                               "weight_footprint_mb + mean(scenario_peak_vram_mb). "
                               "Target 16000 MB / weight 0.20."),
        "vram_finding": finding,
    },
    "model_vram": {"path": "sample", "run_hardware": "L4", "run_quant": "4bit-nf4",
                   "hardware": "L4", "quant": "4bit-nf4"},
    "phase7": {
        "per_model": [{"model": m[0], "accuracy": m[1], "n": 300,
                       "n_correct": round(m[1] * 300)} for m in MODELS],
        "per_class": [],
        "confusion": {},
    },
    "phase8": {
        "weights": WEIGHTS,
        "targets": TARGETS,
        "tiers": TIERS,
        "raw_criteria": raw_rows,
        "normalised": norm_rows,
        "saw_table": merged,
    },
    "sensitivity": {
        "weight_sets": WEIGHT_SETS,
        "ranks": records(sens_ranks),
        "scores": records(sens_scores),
        "top_by_set": top_by_set,
        "top_stable": top_stable,
        "stable_top_model": next(iter(tops)) if top_stable else None,
    },
    "per_agent": {"table": per_agent_table, "dominant": per_agent_dominant},
    "statistics": {},
}

out = Path(__file__).resolve().parents[1] / "dashboard" / "sample_analysis.json"
out.write_text(json.dumps(payload, indent=2))
print("wrote", out)
print("ranking:", [(r["model"], round(r["composite"], 3), r["rank"], r["tier"]) for r in merged])
print("top_stable:", top_stable, "top_by_set:", top_by_set)
