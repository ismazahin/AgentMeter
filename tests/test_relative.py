"""Phase 39: relative (head-to-head) comparison — pure, over synthetic per-model metrics."""
from __future__ import annotations

import pytest

from agentmeter.session.relative import ACC_TIE_PP, REL_TIE, relative_comparison

CAVEATS = ["NON-VALIDATED user run: ...", "Balanced sample: ..."]


def pm(latency, tokens, vram=None, acc=None):
    return {"efficiency": {"end_to_end": {"mean_latency_s": latency, "mean_tokens_per_flow": tokens,
                                          "mean_peak_vram_mb": vram}},
            "accuracy": None if acc is None else {"accuracy": acc}}


def rel(a, b, labelled=None, **kw):
    if labelled is None:                 # labelled iff both models carry accuracy
        labelled = a["accuracy"] is not None and b["accuracy"] is not None
    return relative_comparison(["A", "B"], {"A": a, "B": b}, labelled, CAVEATS, **kw)


def metric(r, key):
    return next(m for m in r["metrics"] if m["metric"] == key)


def test_clear_winner_with_percentages_and_sentence():
    r = rel(pm(2.0, 900, 4000, acc=0.80), pm(4.0, 1000, 5000, acc=0.70), hardware="NVIDIA A100-SXM4-40GB")
    lat, tok, vram = metric(r, "mean_latency_s"), metric(r, "mean_tokens_per_flow"), metric(r, "mean_peak_vram_mb")
    assert (lat["winner"], lat["pct_better"], lat["ratio"]) == ("A", 50.0, 2.0)
    assert (tok["winner"], tok["pct_better"]) == ("A", 10.0)
    assert (vram["winner"], vram["pct_better"]) == ("A", 20.0)
    acc = metric(r, "accuracy")
    assert acc["winner"] == "A" and acc["diff_pp"] == 10.0
    assert r["efficiency_verdict"] == {"verdict": "more_efficient", "model": "A",
                                       "wins": {"A": ["mean_latency_s", "mean_peak_vram_mb",
                                                      "mean_tokens_per_flow"], "B": []},
                                       "rule": "dominance: wins at least one efficiency metric and loses none"}
    assert r["verdict"].startswith("A is more resource-efficient than B: 50.0% lower latency, "
                                   "20.0% less VRAM, 10.0% fewer tokens per flow.")
    assert "Accuracy: A is higher by 10.0 percentage points (A 80.0% vs B 70.0%)." in r["verdict"]
    # honest caveats: like-for-like note, A100 != L4, plus the session's own caveats
    assert any("same hardware and the same flows" in c for c in r["caveats"])
    assert any("NVIDIA A100" in c and "not comparable" in c for c in r["caveats"])
    assert all(c in r["caveats"] for c in CAVEATS)


def test_near_identical_models_tie_instead_of_a_false_winner():
    r = rel(pm(2.00, 1000, 4000, acc=0.800), pm(2.02, 1005, 4030, acc=0.810))
    for key in ("mean_latency_s", "mean_tokens_per_flow", "mean_peak_vram_mb", "accuracy"):
        assert metric(r, key)["winner"] == "tie", key
    assert r["efficiency_verdict"]["verdict"] == "tie" and r["efficiency_verdict"]["model"] is None
    assert r["verdict"].startswith("A and B are tied on resource efficiency")
    assert "Accuracy: tie" in r["verdict"]


def test_thresholds_are_the_documented_ones():
    just_over = rel(pm(1.0, 100), pm(1.0 / (1 - REL_TIE * 1.5), 100))
    assert metric(just_over, "mean_latency_s")["winner"] == "A"
    acc_over = rel(pm(1, 100, acc=0.5 + (ACC_TIE_PP + 1) / 100), pm(1, 100, acc=0.5))
    assert metric(acc_over, "accuracy")["winner"] == "A"


def test_noise_gap_that_is_not_significant_is_a_tie():
    r = rel(pm(2.0, 1000), pm(2.6, 1000), p_values={"mean_latency_s": 0.40})
    lat = metric(r, "mean_latency_s")
    assert lat["winner"] == "tie" and "not statistically significant" in lat["note"]
    sig = rel(pm(2.0, 1000), pm(2.6, 1000), p_values={"mean_latency_s": 0.001})
    assert metric(sig, "mean_latency_s")["winner"] == "A"


def test_mixed_efficiency_and_trade_off_with_accuracy_are_spelled_out():
    r = rel(pm(1.0, 1500, acc=0.60), pm(2.0, 1000, acc=0.60))
    assert r["efficiency_verdict"]["verdict"] == "mixed" and r["efficiency_verdict"]["model"] is None
    assert "A has 50.0% lower latency; B has 33.3% fewer tokens per flow" in r["verdict"]
    t = rel(pm(1.0, 1000, acc=0.50), pm(2.0, 1000, acc=0.90))
    assert "Trade-off: A is more efficient, B is more accurate." in t["verdict"]


def test_unlabelled_is_efficiency_only_and_vram_can_be_unavailable():
    r = rel(pm(1.0, 1000), pm(1.5, 1000), labelled=False)
    assert r["accuracy_included"] is False and r["accuracy_verdict"] is None
    assert all(m["metric"] != "accuracy" for m in r["metrics"])
    assert "Accuracy: not available (no ground-truth labels) — efficiency only." in r["verdict"]
    vram = metric(r, "mean_peak_vram_mb")
    assert vram["available"] is False and vram["winner"] is None
    assert "(mean peak working VRAM: not available)" in r["verdict"]


def test_only_for_two_models():
    assert relative_comparison(["A"], {"A": pm(1, 1)}, True, CAVEATS) is None


@pytest.mark.parametrize("hw, flagged", [("NVIDIA L4", False), ("cpu", True), (None, False)])
def test_hardware_caveat(hw, flagged):
    r = rel(pm(1, 1), pm(1, 1), hardware=hw)
    assert any("not comparable with the locked study" in c for c in r["caveats"]) is flagged


def test_an_untestable_p_value_never_declares_a_winner():
    r = rel(pm(2.0, 1000, vram=1000), pm(2.0, 1000, vram=1300), p_values={"mean_peak_vram_mb": float("nan")})
    v = metric(r, "mean_peak_vram_mb")
    assert v["winner"] == "tie" and v["p_value"] is None and "could not be tested" in v["note"]


# ---------------------------------------------------------------- 3 or 4 models (Phase H)
def multi(models, pair_p=None, labelled=None):
    names = list(models)
    if labelled is None:
        labelled = all(m["accuracy"] is not None for m in models.values())
    return relative_comparison(names, models, labelled, CAVEATS, pair_p=pair_p)


def test_three_models_one_beats_every_other():
    r = multi({"A": pm(1.0, 500, acc=0.9), "B": pm(2.0, 800, acc=0.7), "C": pm(3.0, 900, acc=0.8)})
    assert r["mode"] == "multi" and r["models"] == ["A", "B", "C"] and len(r["pairs"]) == 3
    lat = metric(r, "mean_latency_s")
    assert (lat["winner"], lat["runner_up"], lat["pct_better"]) == ("A", "B", 50.0)
    assert lat["values"] == {"A": 1.0, "B": 2.0, "C": 3.0}
    ev = r["efficiency_verdict"]
    assert ev["verdict"] == "more_efficient" and ev["model"] == "A" and set(ev["beats"]["A"]) == {"B", "C"}
    assert [x["model"] for x in r["ranking"]] == ["A", "B", "C"] and r["ranking"][0]["pair_wins"] == 2
    assert r["verdict"].startswith("A is more resource-efficient than each of the other 2 models. Best per metric: lower latency: A (50.0% ahead of B)")
    acc = metric(r, "accuracy")
    assert (acc["winner"], acc["runner_up"], acc["diff_pp"]) == ("A", "C", 10.0)
    assert "Accuracy: A is highest, 10.0 points ahead of C (A 90.0%, C 80.0%, B 70.0%)." in r["verdict"]
    # every pair is the ordinary 2-model comparison
    ab = next(p for p in r["pairs"] if p["models"] == ["A", "B"])
    assert ab["efficiency_verdict"]["model"] == "A" and "pairs" not in ab


def test_four_models_mixed_when_nobody_beats_all():
    r = multi({"A": pm(1.0, 900), "B": pm(2.0, 500), "C": pm(3.0, 950), "D": pm(4.0, 990)})
    ev = r["efficiency_verdict"]
    assert ev["verdict"] == "mixed" and ev["model"] is None
    assert ev["wins"] == {"A": ["mean_latency_s"], "B": ["mean_tokens_per_flow"], "C": [], "D": []}
    assert r["verdict"].startswith("Mixed efficiency across the 4 models: no model beats every other one.")
    assert "Accuracy: not available" in r["verdict"] and len(r["pairs"]) == 6
    assert [x["model"] for x in r["ranking"]][-1] == "D"


def test_multi_uses_pairwise_significance():
    # A is 10% faster than B, but the Dunn test for that pair says noise -> no clear best on latency
    p = {"mean_latency_s": {("A", "B"): 0.4, ("A", "C"): 0.001, ("B", "C"): 0.001}}
    r = multi({"A": pm(1.0, 500), "B": pm(1.1, 500), "C": pm(3.0, 500)}, pair_p=p)
    lat = metric(r, "mean_latency_s")
    assert lat["winner"] == "tie" and lat["p_value"] == 0.4 and lat["runner_up"] == "B"
    ab = next(x for x in r["pairs"] if x["models"] == ["A", "B"])
    assert metric(ab, "mean_latency_s")["winner"] == "tie"
    ac = next(x for x in r["pairs"] if x["models"] == ["A", "C"])
    assert metric(ac, "mean_latency_s")["winner"] == "A"
    assert r["efficiency_verdict"]["verdict"] == "mixed"       # A does not beat B
    assert "latency per flow: no clear best" in r["verdict"]


def test_multi_all_tied_and_missing_vram():
    r = multi({"A": pm(1.0, 500, 4000), "B": pm(1.0, 501, None), "C": pm(1.005, 500, 4000)})
    assert r["efficiency_verdict"]["verdict"] == "tie" and r["verdict"].startswith("The 3 models are tied")
    vram = metric(r, "mean_peak_vram_mb")
    assert vram["available"] is False and "not measured for every model" in vram["note"]
