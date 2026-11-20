from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.analysis import rca, slo
from lab.analysis.detect import Detector, Rule, alert_intervals, detector_from_dict, evaluate, score

TARGET = 0.99
THR = {"short": 2.0, "medium": 4.0, "long": 10.0}
WEIGHTS = {"short": 100, "medium": 500, "long": 1500}


def synth(duration=600, rps=5.0, bad=lambda i, t, cls: False, seed=0, mix=(0.6, 0.3, 0.1)) -> pd.DataFrame:
    """Deterministic request log: evenly spaced arrivals, classes drawn with a seeded RNG."""
    rng = np.random.default_rng(seed)
    n = int(duration * rps)
    rows = []
    for i in range(n):
        t = (i + 0.5) / rps
        cls = rng.choice(["short", "medium", "long"], p=list(mix))
        is_bad = bad(i, t, cls)
        rows.append({"idx": i, "cls": cls, "t_sched": t, "t_send": t, "t_done": t + 0.5, "weight": WEIGHTS[cls],
                     "status": "ok", "ttft_s": THR[cls] * (1.5 if is_bad else 0.2), "total_s": 1.0,
                     "retrieve_s": 0.01, "llm_ttft_s": 0.2, "prompt_tokens": 10, "completion_tokens": 5})
    return slo.annotate(pd.DataFrame(rows), THR)


def test_annotate_good_definition():
    df = pd.DataFrame([
        {"cls": "short", "status": "ok", "ttft_s": 1.9, "t_done": 1, "weight": 1},
        {"cls": "short", "status": "ok", "ttft_s": 2.1, "t_done": 2, "weight": 1},
        {"cls": "long", "status": "ok", "ttft_s": 9.0, "t_done": 3, "weight": 1},
        {"cls": "short", "status": "timeout", "ttft_s": None, "t_done": 4, "weight": 1},
        {"cls": "short", "status": "llm_error", "ttft_s": 0.1, "t_done": 5, "weight": 1},
    ])
    assert slo.annotate(df, THR)["good"].tolist() == [True, False, True, False, False]


def test_burn_is_zero_without_failures():
    df = synth()
    grid = np.arange(10, 600, 5.0)
    for kind in ("request", "token", "class"):
        assert np.allclose(slo.burn_series(df, 60, grid, kind, TARGET), 0.0)


def test_burn_equals_bad_fraction_over_budget():
    df = synth(bad=lambda i, t, c: 200 <= t < 320 and i % 2 == 0)  # exactly half of requests bad in the incident
    grid = np.array([250.0, 300.0])  # windows of 30 s fully inside the incident
    burn = slo.burn_series(df, 30, grid, "request", TARGET)
    assert burn == pytest.approx([50.0, 50.0], rel=0.08)  # 0.5 / 0.01
    assert slo.burn_series(df, 30, np.array([100.0]), "request", TARGET)[0] == 0.0


def test_token_sli_amplifies_long_prompt_failures():
    """H1 mechanism: if only long prompts fail, the request SLI sees 10% bad but the cost-weighted SLI sees ~42%."""
    df = synth(duration=900, bad=lambda i, t, c: c == "long" and t >= 100)
    grid = np.array([800.0])
    req = slo.burn_series(df, 300, grid, "request", TARGET)[0]
    tok = slo.burn_series(df, 300, grid, "token", TARGET)[0]
    cls = slo.burn_series(df, 300, grid, "class", TARGET)[0]

    # Independent expectation computed by hand from the REALISED window (not the nominal 10% mix).
    win = df[(df["t_done"] > 500) & (df["t_done"] <= 800)]
    is_long = win["cls"] == "long"
    expect_req = is_long.mean() / (1 - TARGET)
    expect_tok = win.loc[is_long, "weight"].sum() / win["weight"].sum() / (1 - TARGET)
    assert req == pytest.approx(expect_req, rel=1e-9)
    assert tok == pytest.approx(expect_tok, rel=1e-9)
    assert 6.0 < req < 14.0                      # sanity: roughly the nominal 10% long share
    assert tok > 3.5 * req                       # the token SLI sees the cost-weighted failure much louder (H1 mechanism)
    assert cls == pytest.approx(100.0, rel=1e-9)  # every long request is bad => worst-class burn = 1/0.01


def test_min_events_guard_suppresses_sparse_windows():
    df = synth(duration=100, rps=0.05, bad=lambda i, t, c: True)  # 5 events total, all bad
    grid = np.array([99.0])
    assert slo.burn_series(df, 100, grid, "request", TARGET, min_events=10)[0] == 0.0
    assert slo.burn_series(df, 100, grid, "request", TARGET, min_events=3)[0] > 0.0


def test_events_count_only_after_completion():
    df = synth(duration=60, rps=2.0, bad=lambda i, t, c: t > 30)
    assert slo.burn_series(df, 10, np.array([30.4]), "request", TARGET)[0] == 0.0  # t_done = t + 0.5
    assert slo.burn_series(df, 10, np.array([40.0]), "request", TARGET)[0] > 0.0


def test_budget_consumed_and_class_stats():
    df = synth(bad=lambda i, t, c: i % 20 == 0)  # 5% bad
    assert slo.budget_consumed(df, TARGET, 0, 600) == pytest.approx(5.0, rel=0.05)
    st = slo.class_stats(df)
    assert st["all"]["n"] == 3000
    assert st["short"]["ttft_p50"] == pytest.approx(0.4, rel=0.01)
    assert st["all"]["good_ratio"] == pytest.approx(0.95, rel=0.01)


GOOGLE = Detector("g", "request", (Rule(60, 5, 14.4), Rule(360, 30, 6)))
TUNED = Detector("t", "request", (Rule(120, 20, 6), Rule(300, 60, 2.5)))


def test_fast_incident_is_detected_quickly():
    df = synth(bad=lambda i, t, c: 200 <= t < 320 and i % 2 == 0)
    grid, fire = evaluate(df, GOOGLE, TARGET, 600)
    res = score(alert_intervals(grid, fire), [{"start": 200, "end": 320}], 600)
    assert res["detected"] and 10 <= res["ttd_s"] <= 35
    assert res["false_pages"] == 0


def test_slow_burn_is_missed_by_fast_rules_but_caught_by_tuned():
    df = synth(bad=lambda i, t, c: 120 <= t < 480 and i % 33 == 0)  # ~3% bad => burn ~3
    inc = [{"start": 120, "end": 480}]
    g = score(alert_intervals(*evaluate(df, GOOGLE, TARGET, 600)), inc, 600)
    t = score(alert_intervals(*evaluate(df, TUNED, TARGET, 600)), inc, 600)
    assert not g["detected"]
    assert t["detected"] and 150 <= t["ttd_s"] <= 340


def test_no_false_pages_on_quiet_traffic():
    df = synth(bad=lambda i, t, c: i % 500 == 0)  # 0.2% bad => burn 0.2
    for det in (GOOGLE, TUNED):
        res = score(alert_intervals(*evaluate(df, det, TARGET, 600)), [], 600)
        assert res["pages_total"] == 0 and res["false_pages_per_hour"] == 0.0


def test_startup_spike_does_not_page_but_real_incident_right_after_does():
    """One bad request among the first ~12 events must not page (empty-history artefact)."""
    df = synth(bad=lambda i, t, c: i == 0)
    for det in (GOOGLE, TUNED):
        assert score(alert_intervals(*evaluate(df, det, TARGET, 600)), [], 600)["pages_total"] == 0
    # ...whereas disabling the guard shows the artefact really exists (so the guard is doing the work)
    grid, fire = evaluate(df, GOOGLE, TARGET, 600, min_history_s=0.0)
    assert fire.any()
    # and a genuine incident that starts after the guard has expired is still detected
    df2 = synth(bad=lambda i, t, c: 100 <= t < 220 and i % 2 == 0)
    res = score(alert_intervals(*evaluate(df2, GOOGLE, TARGET, 600)), [{"start": 100, "end": 220}], 600)
    assert res["detected"] and res["ttd_s"] < 40


def test_alert_intervals_and_scoring_edges():
    grid = np.arange(1.0, 11.0)
    fire = np.array([0, 1, 1, 0, 0, 1, 0, 0, 0, 1], dtype=bool)
    assert alert_intervals(grid, fire) == [(2.0, 3.0), (6.0, 6.0), (10.0, 10.0)]
    res = score([(2.0, 3.0), (6.0, 6.0)], [{"start": 5, "end": 7}], 10, grace_s=1)
    assert res["false_pages"] == 1 and res["detected"] and res["ttd_s"] == 1.0
    # an alert that starts BEFORE the incident is a false page, not a detection
    res2 = score([(1.0, 9.0)], [{"start": 5, "end": 7}], 10)
    assert not res2["detected"] and res2["false_pages"] == 1


def test_detector_from_dict_roundtrip():
    d = detector_from_dict({"name": "x", "sli": "token", "min_events": 3, "rules": [{"long_s": 60, "short_s": 5, "burn": 14.4}]})
    assert d.sli == "token" and d.min_events == 3 and d.rules[0] == Rule(60, 5, 14.4)


# ---------------------------------------------------------------- RCA baseline
def stage_df(inc_retrieve=0.01, inc_llm=0.2, inc_decode=0.3, n=400):
    rows = []
    for i in range(n):
        t = i * 0.5
        in_inc = 100 <= t < 160
        r = inc_retrieve if in_inc else 0.01
        lt = inc_llm if in_inc else 0.2
        dec = inc_decode if in_inc else 0.3
        ttft = r + lt + 0.005
        rows.append({"t_send": t, "status": "ok", "retrieve_s": r, "llm_ttft_s": lt, "ttft_s": ttft, "total_s": ttft + dec})
    return pd.DataFrame(rows)


@pytest.mark.parametrize(
    "kwargs,truth",
    [({"inc_retrieve": 1.5}, "retriever"), ({"inc_llm": 2.0}, "llm"), ({"inc_decode": 2.0}, "llm")],
)
def test_rca_localizes_the_degraded_stage(kwargs, truth):
    res = rca.localize(stage_df(**kwargs), (20.0, 90.0), (100.0, 160.0))
    assert res["pred"] == truth and not res["abstain"]
    assert rca.score_methods(res["pred"], truth)["attribution_correct"]


def test_rca_abstains_when_nothing_changed_or_no_data():
    assert rca.localize(stage_df(), (20.0, 90.0), (100.0, 160.0))["pred"] is None
    df = stage_df(inc_retrieve=1.5)
    df.loc[(df.t_send >= 100) & (df.t_send < 160), "status"] = "timeout"  # full outage => no stage data
    res = rca.localize(df, (20.0, 90.0), (100.0, 160.0))
    assert res["abstain"] and res["pred"] is None


def test_replica_timeline_reports_scale_up_ready_lag_and_scale_down(tmp_path):
    from lab.analysis.pipeline import replica_timeline
    rows = ["epoch,spec,ready"]
    for i, (spec, ready) in enumerate([(1, 1)] * 5 + [(2, 1)] * 2 + [(2, 2)] * 4 + [(1, 2)] + [(1, 1)] * 3):
        rows.append(f"{1000 + 5 * i},{spec},{ready}")
    (tmp_path / "replicas.csv").write_text("\n".join(rows) + "\n")
    assert replica_timeline(tmp_path, 1000.0) == [(0.0, 1, 1), (25.0, 2, 1), (35.0, 2, 2), (55.0, 1, 2), (60.0, 1, 1)]
    assert replica_timeline(tmp_path / "nope", 0.0) == []
