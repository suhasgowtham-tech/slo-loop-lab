"""lab/analysis/robust.py: paired statistics and the with/without-accepted-runs tables (v2.6.0).

The numbers a hypothesis is judged on come out of this module, so the arithmetic is checked against hand-computed values, and the one thing that
went wrong in practice (acceptance matched by run NAME while the same name exists in both phases) has its own end-to-end test.
"""
from __future__ import annotations

import csv
import json
import math

import pandas as pd

from conftest import ROOT
from lab import cli, gate
from lab import config as cfg
from lab.analysis import robust
from test_v25 import make_run


# ------------------------------------------------------------------------------------------------------------------- statistics
def test_wilson_interval_contains_the_rate_stays_in_0_1_and_is_symmetric_at_one_half():
    assert all(math.isnan(x) for x in robust.wilson(0, 0))
    lo, hi = robust.wilson(5, 10)
    assert abs((lo + hi) / 2 - 0.5) < 1e-9 and 0.23 < lo < 0.24 and 0.76 < hi < 0.77
    for k, n in ((0, 13), (1, 13), (13, 13), (37, 49)):
        lo, hi = robust.wilson(k, n)
        assert 0.0 <= lo <= k / n <= hi <= 1.0
    assert robust.wilson(0, 13)[0] == 0.0 and robust.wilson(13, 13)[1] == 1.0          # zero and all are bounded, not fake-certain


def test_sign_test_matches_hand_computed_exact_values():
    assert robust.sign_test_p(0, 0) == 1.0
    assert abs(robust.sign_test_p(5, 0) - 2 / 32) < 1e-12                              # 5 vs 0: 2 * (1/32)
    assert abs(robust.sign_test_p(8, 2) - 2 * (1 + 10 + 45) / 1024) < 1e-12           # 8 vs 2: 0.109375
    assert robust.sign_test_p(3, 3) == 1.0 and robust.sign_test_p(2, 8) == robust.sign_test_p(8, 2)


def _det(rows):
    """rows: (run, scenario, detector, detected, ttd_s) -> the columns the module reads."""
    return pd.DataFrame([{"run": r, "phase": "A", "scenario": sc, "detector": d, "has_incident": True, "detected": f, "ttd_s": t,
                          "false_pages": 0, "quiet_hours": 0.1} for r, sc, d, f, t in rows])


def test_paired_detection_counts_who_found_it_alone_and_how_much_sooner():
    rows = []
    for i in range(1, 9):       # runs 1-8: the class SLI finds all, the request SLI finds 1-5; both found 1-5, class sooner by 100 s
        rows.append((f"r{i}", "s7_slow_burn", "tuned_class", True, 150.0 + i))
        rows.append((f"r{i}", "s7_slow_burn", "tuned_request", i <= 5, 250.0 + i if i <= 5 else None))
    r = robust.paired_detection(_det(rows), "tuned_class", "tuned_request")
    assert (r["runs"], r["a_found"], r["b_found"], r["a_only"], r["b_only"], r["both"]) == (8, 8, 5, 3, 0, 5)
    assert abs(r["p_sign"] - 0.25) < 1e-12 and r["ttd_a_minus_b_median_s"] == -100.0
    assert robust.paired_detection(_det(rows), "tuned_class", "nope") is None


def test_detector_summary_pools_and_splits_s7_from_the_rest():
    rows = [(f"r{i}", "s7_slow_burn", "d", i <= 4, 100.0) for i in range(1, 9)] + [(f"q{i}", "s1_burst", "d", True, 10.0) for i in range(1, 5)]
    s = robust.detector_summary(_det(rows)).iloc[0]
    assert s["found"] == "8/12" and s["s7_slow_burn"] == "4/8" and s["all_but_s7"] == "4/4"
    assert abs(s["macro_rate"] - 0.75) < 1e-12           # (0.5 + 1.0) / 2: every scenario weighs the same, not every run


def _summ(rows):
    return pd.DataFrame([{"run": f"{sc}__{p}__r{rep}", "phase": "B", "scenario": sc, "policy": p, "rep": rep, "budget_consumed_request": b,
                          "mean_ready_replicas": rr} for sc, p, rep, b, rr in rows])


def test_paired_budget_counts_wins_ties_and_the_replica_price():
    rows = []
    for rep, (cpu_b, queue_b) in enumerate([(0.0, 8.0), (1.0, 9.0), (0.0, 0.0), (2.0, 5.0)], start=1):
        rows += [("s3", "cpu", rep, cpu_b, 2.5), ("s3", "queue", rep, queue_b, 1.2)]
    r = robust.paired_budget(_summ(rows), "cpu", "queue")
    assert (r["pairs"], r["a_burns_less"], r["a_burns_more"], r["ties"]) == (4, 3, 0, 1)
    assert abs(r["p_sign"] - 0.25) < 1e-12 and abs(r["mean_budget_diff"] - (-(8 + 8 + 0 + 3) / 4)) < 1e-12
    assert abs(r["mean_replica_diff"] - 1.3) < 1e-12
    assert robust.paired_budget(_summ(rows), "cpu", "burn") is None


# ------------------------------------------------------------------------------------------------------ phase-aware exclusion
def test_without_drops_by_phase_and_name_not_by_name_alone():
    df = pd.DataFrame({"phase": ["A", "B", "A", "B"], "run": ["x__static1__r2", "x__static1__r2", "y__static1__r1", "y__static1__r1"], "v": [1, 2, 3, 4]})
    out = robust._without(df, {("B", "x__static1__r2")})
    assert sorted(zip(out["phase"], out["run"])) == [("A", "x__static1__r2"), ("A", "y__static1__r1"), ("B", "y__static1__r1")]
    assert robust._without(df, set()) is df


def test_early_detections_lists_only_pages_within_30_s_and_marks_accepted_runs():
    det = _det([("r1", "s7_slow_burn", "google_x60_request", True, 8.0), ("r2", "s7_slow_burn", "google_x60_request", True, 280.0),
                ("r1", "s7_slow_burn", "tuned_class", False, None), ("r3", "s7_slow_burn", "tuned_class", True, 21.0)])
    e = robust.early_detections(det, {("A", "r1")})
    assert list(zip(e["run"], e["detector"], e["accepted"])) == [("r1", "google_x60_request", True), ("r3", "tuned_class", False)]
    assert robust.early_detections(det[det["ttd_s"] > 100], set()).empty
    fast = _det([("n1", "s6_netem_delay", "tuned_token", True, 12.0)])               # a fast-onset scenario: a page within seconds is legitimate
    assert robust.early_detections(fast, set()).empty
    assert len(robust.early_detections(fast, set(), scenarios=("s6_netem_delay",))) == 1


# ------------------------------------------------------------------------------------------------------------------- end to end
def _mk(runs, phase, scenario, policy, rep, bad):
    (runs / phase).mkdir(parents=True, exist_ok=True)
    d = make_run(runs / phase, name=f"{scenario}__{policy}__r{rep}", policy=policy, bad=bad, rps=2.0,
                 replicas=[(1000.0, 1, 1), (1300.0, 2, 2), (1600.0, 2, 2)])
    m = json.loads((d / "meta.json").read_text())
    m.update(phase=phase, rep=rep, scenario=scenario)
    (d / "meta.json").write_text(json.dumps(m))
    return d


def _world(tmp_path, monkeypatch, accepted_rows):
    runs = tmp_path / "results" / "runs"
    in_incident = lambda t: 200 <= t < 320 and int(t * 2) % 2 == 0  # noqa: E731
    for rep in (1, 2, 3):                                              # Phase A: the same detectors on three s7 runs
        _mk(runs, "A", "s7_slow_burn", "static1", rep, in_incident)
    for rep in (1, 2, 3):                                              # Phase B: the SAME names exist here (static1) next to a second policy
        _mk(runs, "B", "s7_slow_burn", "static1", rep, in_incident)
        _mk(runs, "B", "s7_slow_burn", "cpu", rep, lambda t: False)
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    path = runs.parent / gate.ACCEPT_NAME
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["epoch", "phase", "run", "flags", "attempts", "note"])
        for ph, name in accepted_rows:
            w.writerow([1, ph, name, "SLOW", 1, "test"])
    return runs


def _cell_n(cell: str) -> int:
    return int(cell.split()[0].split("/")[1])


def test_build_robust_leaves_out_only_the_accepted_run_of_that_phase(tmp_path, monkeypatch):
    runs = _world(tmp_path, monkeypatch, [("B", "s7_slow_burn__static1__r2")])      # accepted in Phase B only; Phase A has the same name
    text = robust.build_robust()
    out = runs.parent
    assert "minus_accepted drops 1 run(s)" in text
    for f in ("ROBUSTNESS.md", "robust_detectors.csv", "robust_detectors_by_variant.csv", "robust_paired_h1.csv", "robust_paired_h2.csv",
              "robust_rca.csv", "robust_budget_by_variant.csv", "robust_paired_budget.csv"):
        assert (out / f).exists(), f
    sens = pd.read_csv(out / "robust_detectors_by_variant.csv")
    assert {_cell_n(c) for c in sens["all"]} == {3} and {_cell_n(c) for c in sens["minus_accepted"]} == {3}   # Phase A untouched
    bud = pd.read_csv(out / "robust_budget_by_variant.csv")
    st1, cpu = bud[bud["policy"] == "static1"].iloc[0], bud[bud["policy"] == "cpu"].iloc[0]
    assert (st1["n"], st1["n_minus_accepted"]) == (3, 2) and (cpu["n"], cpu["n_minus_accepted"]) == (3, 3)   # Phase B loses exactly one run


def test_build_robust_phase_a_acceptance_removes_the_phase_a_run(tmp_path, monkeypatch):
    runs = _world(tmp_path, monkeypatch, [("A", "s7_slow_burn__static1__r2")])
    robust.build_robust()
    sens = pd.read_csv(runs.parent / "robust_detectors_by_variant.csv")
    assert {_cell_n(c) for c in sens["all"]} == {3} and {_cell_n(c) for c in sens["minus_accepted"]} == {2}
    bud = pd.read_csv(runs.parent / "robust_budget_by_variant.csv")
    assert set(bud["n"]) == {3} and set(bud["n_minus_accepted"]) == {3}               # Phase B untouched


def test_robust_command_and_make_target(tmp_path, monkeypatch, capsys):
    _world(tmp_path, monkeypatch, [])
    assert cli.main(["robust"]) == 0
    out = capsys.readouterr().out
    assert "SLO-LOOP LAB DIGEST" in out and "== PHASE A: detectors" in out and "== PHASE B: H3 policy against policy" in out and "[robust] full report" in out
    assert max(len(ln) for ln in out.splitlines() if not ln.startswith(("left out", "found =", "ttd_diff_s", "budget:", "SLO-LOOP", "[robust]"))) < 140   # fits one screenshot
    assert (cfg.RUNS_DIR.parent / robust.DIGEST_NAME).exists()
    assert any(ln.startswith("robust:") for ln in (ROOT / "Makefile").read_text().splitlines()) and "make robust" in (ROOT / "README.md").read_text()


# ------------------------------------------------------------------------------------- pre-registered confirmation (fresh reps only)
def _rca(tagged_ok, reps_s5=range(1, 13), reps_s7=range(1, 26)):
    """tagged_ok(scenario, rep) -> bool. Old reps (s5 r1-r6, s7 r1-r13) are always 'tagged right' so that any leak into the fresh table is visible."""
    rows = []
    for sc, reps in (("s5_retriever_latency", reps_s5), ("s7_slow_burn", reps_s7)):
        for rep in reps:
            old = rep < robust.FRESH[("A", sc)]
            rows.append({"run": f"{sc}__static1__r{rep}", "phase": "A", "scenario": sc, "truth": "retriever", "attribution_correct": False,
                         "attribution_abstained": False, "always_llm_correct": False, "random_expected": 1 / 3, "adjusted_correct": False,
                         "tagged_correct": True if old else tagged_ok(sc, rep)})
    return pd.DataFrame(rows)


def test_fresh_only_keeps_only_reps_that_did_not_exist_at_the_first_reading():
    f = robust.fresh_only(_rca(lambda *_: True), "A")
    assert len(f) == 18 and set(f[f["scenario"] == "s7_slow_burn"]["run"].map(robust._rep_of)) == set(range(14, 26))
    assert set(f[f["scenario"] == "s5_retriever_latency"]["run"].map(robust._rep_of)) == set(range(7, 13))
    assert robust.fresh_only(_rca(lambda *_: True), "B").empty                              # no Phase B rule applies to Phase A scenarios


def test_h4_is_confirmed_only_when_the_wilson_lower_bound_clears_chance_on_fresh_runs_alone():
    tab, lines = robust.confirm_h4(_rca(lambda sc, rep: True), set())
    assert tab[tab["group"] == "fresh pooled"].iloc[0]["incidents"] == 18                    # the 19 old 'correct' runs did not leak in
    assert any("failure_tagged 18/18" in x and x.endswith("-> CONFIRMED") for x in lines)
    _, lines = robust.confirm_h4(_rca(lambda sc, rep: rep % 5 in (0, 1)), set())            # 7 of 18 right: lower bound ~0.20 < 1/3
    assert any("-> NOT CONFIRMED" in x for x in lines)
    _, lines = robust.confirm_h4(_rca(lambda sc, rep: True, reps_s5=range(1, 10), reps_s7=range(1, 20)), set())
    assert any("INCOMPLETE: 9 of 18 fresh incidents" in x for x in lines)                    # an unfinished set is never called confirmed


def test_h4_without_accepted_drops_exactly_that_run_and_can_change_the_verdict():
    tab, lines = robust.confirm_h4(_rca(lambda sc, rep: True), {("A", "s7_slow_burn__static1__r16")})
    assert any("fresh pooled, without accepted]: failure_tagged 17/17" in x for x in lines)
    assert tab[tab["group"] == "fresh pooled, without accepted"].iloc[0]["incidents"] == 17
    assert robust.confirm_h4(_rca(lambda *_: True).iloc[0:0], set())[0].empty


def _budget(cpu, queue, burn, reps=range(1, 6)):
    rows = []
    for sc in robust.H3_SCENARIOS + ["s2_long_shift"]:
        for rep in reps:
            for pol, b, rr in (("cpu", cpu, 2.6), ("queue", queue, 1.2), ("burn", burn, 1.5)):
                rows.append({"run": f"{sc}__{pol}__r{rep}", "phase": "B", "scenario": sc, "policy": pol, "rep": rep,
                             "budget_consumed_request": 0.0 if sc == "s2_long_shift" else b, "mean_ready_replicas": rr})
    return pd.DataFrame(rows)


def test_h3_registered_rule_and_the_fresh_pairs_that_cannot_reach_significance_by_themselves():
    tab, lines = robust.confirm_h3(_budget(0.3, 8.0, 11.0), set())
    reg = next(x for x in lines if x.startswith("H3 [registered rule: reps 1-5 (includes"))
    assert reg.endswith("-> CONFIRMED") and "cpu < queue: 10/10 non-tied" in reg and "cpu < burn: 10/10 non-tied" in reg
    fresh = tab[tab["scope"] == "fresh reps 4+ only"]
    assert set(fresh["pairs"]) == {"4/4"} and set(fresh["rule_met"]) == {False}               # 4 of 4 in the right direction, still p = 0.125
    assert abs(fresh.iloc[0]["p_sign"] - 0.125) < 1e-12 and abs(fresh.iloc[0]["min_p_possible"] - 0.125) < 1e-12
    assert any("fresh reps 4+ only" in x and "direction replicated" in x and "cannot reach p < 0.05 (smallest possible p = 0.125)" in x for x in lines)


def test_h3_is_not_confirmed_when_cpu_loses_a_pair_too_often_and_incomplete_when_runs_are_missing():
    df = _budget(0.3, 8.0, 11.0)
    flip = (df["scenario"] == "s3_cpu_stress") & (df["policy"] == "cpu") & (df["rep"].isin([1, 2, 3]))
    df.loc[flip, "budget_consumed_request"] = 50.0                                          # cpu worse in 3 of 10 pairs: 70% < 80%
    _, lines = robust.confirm_h3(df, set())
    assert any(x.startswith("H3 [registered rule: reps 1-5 (includes") and "-> NOT CONFIRMED" in x for x in lines)
    _, lines = robust.confirm_h3(_budget(0.3, 8.0, 11.0, reps=range(1, 4)), set())
    assert any("INCOMPLETE: 6 of 10 pairs" in x for x in lines) and not any("fresh reps 4+ only" in x for x in lines)      # no fresh reps yet: no fresh line
    assert robust.confirm_h3(pd.DataFrame(), set())[0].empty


def test_digest_starts_with_the_confirmation_verdicts_before_any_other_table(tmp_path, monkeypatch):
    runs = _world(tmp_path, monkeypatch, [])
    robust.build_robust()
    d = (runs.parent / robust.DIGEST_NAME).read_text()
    assert d.index("== CONFIRMATION on fresh reps") < d.index("== PHASE A: detectors")
    assert "H4: no fresh Phase A runs yet" in d


def test_h3_with_a_sixth_rep_the_fresh_pairs_alone_can_reach_significance():
    df = _budget(0.3, 8.0, 11.0, reps=range(1, 7))                                           # reps 4-6 of s3 and s8 = 6 fresh pairs
    tab, lines = robust.confirm_h3(df, set())
    fresh = tab[tab["scope"] == "fresh reps 4+ only"]
    assert set(fresh["pairs"]) == {"6/6"} and set(fresh["rule_met"]) == {True} and abs(fresh.iloc[0]["p_sign"] - 2 / 64) < 1e-12
    assert any("significant at 0.05 on fresh pairs alone" in x for x in lines)
    pooled = tab[tab["scope"].str.startswith("registered rule: reps 1-5")]
    assert set(pooled["pairs"]) == {"10/10"}                                                 # a 6th rep never changes the registered rule
