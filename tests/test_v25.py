"""v2.5: the tools that make the open questions of the Phase A data answerable, and the autoscaler-threshold overlay.

Everything here is offline (no cluster).
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest
import yaml

from conftest import ROOT
from lab import cli, k8s, sim
from lab import config as cfg
from lab.analysis import pipeline, rca, report
from lab.analysis.detect import Detector, Rule

read = lambda p: (ROOT / p).read_text()  # noqa: E731
THR = {"short": 10.4, "medium": 15.6, "long": 16.3}


# ------------------------------------------------------------------------------------------- attribution estimators
def mixed_df(retrieve_extra=0.0, n=60, long_share_base=0.1, long_share_inc=0.6, seed=1):
    """Baseline 20-90 s with few long prompts, incident 100-160 s with MANY long prompts (pure prompt-mix noise).

    Per class the llm stage is identical in both windows; only `retrieve_extra` (seconds) differs inside the incident."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n * 6):
        t = 20 + i * (140.0 / (n * 6))
        inc = 100 <= t < 160
        share = long_share_inc if inc else long_share_base
        cls = "long" if rng.random() < share else "short"
        llm = 4.0 if cls == "long" else 0.5
        r = 0.03 + (retrieve_extra if inc else 0.0)
        rows.append({"t_send": t, "cls": cls, "status": "ok", "retrieve_s": r, "llm_ttft_s": llm,
                     "ttft_s": r + llm + 0.002, "total_s": r + llm + 1.0})
    return pd.DataFrame(rows)


def test_pooled_estimator_is_fooled_by_prompt_mix_but_class_adjusted_is_not():
    df = mixed_df(retrieve_extra=1.5)          # truth: retriever (+1.5 s); the window also has far more long prompts
    base, inc = (20.0, 90.0), (100.0, 160.0)
    assert rca.localize(df, base, inc)["pred"] == "llm"                # observed in the real data: retriever fault, llm blamed
    adj = rca.localize_adjusted(df, base, inc)
    assert adj["pred"] == "retriever" and adj["scores"]["llm"] == pytest.approx(0.0, abs=1e-9)
    assert adj["scores"]["retriever"] == pytest.approx(1.5, abs=0.05)


def test_class_adjusted_abstains_when_only_the_prompt_mix_changed():
    df = mixed_df(retrieve_extra=0.0)
    res = rca.localize_adjusted(df, (20.0, 90.0), (100.0, 160.0))
    assert res["pred"] is None and res["abstain"]


def test_class_adjusted_works_without_a_class_column_and_with_rare_classes():
    df = mixed_df(retrieve_extra=1.5).drop(columns="cls")
    assert rca.localize_adjusted(df, (20.0, 90.0), (100.0, 160.0))["pred"] in {"retriever", "llm"}  # no cls column: one pooled class, no crash
    df = mixed_df(retrieve_extra=1.5, long_share_base=0.0)             # no long request at all in the baseline
    res = rca.localize_adjusted(df, (20.0, 90.0), (100.0, 160.0))
    assert res["pred"] == "retriever"                                  # unadjustable long rows are left out, NOT compared with the pooled median
    assert res["n_inc"] < len(df[(df.t_send >= 100) & (df.t_send < 160)])
    only_long = df.copy()
    only_long["cls"] = "long"
    only_long.loc[only_long.t_send < 100, "cls"] = "short"             # every incident row is a class the baseline never had
    assert rca.localize_adjusted(only_long, (20.0, 90.0), (100.0, 160.0))["abstain"]


def test_failure_component_maps_what_the_client_really_records():
    # the gateway's retriever failure is an HTTP 503 and reaches the load generator as "http_503"
    assert rca.failure_component("http_503") == "retriever" and rca.failure_component("retriever_error") == "retriever"
    for s in ("llm_error", "llm_incomplete", "timeout", "llm_http_500"):
        assert rca.failure_component(s) == "llm"
    for s in ("ok", "client_error", "client_timeout", "client_drop", "http_502", None, float("nan")):
        assert rca.failure_component(s) is None


def _fail_df(inc_status, n_inc_ok=0, base_status=None):
    rows = []
    for i in range(12):
        t = 20 + 5 * i
        st = base_status if (base_status and i == 3) else "ok"
        rows.append({"t_send": t, "cls": "short", "status": st, "retrieve_s": 0.03 if st == "ok" else None,
                     "llm_ttft_s": 0.5 if st == "ok" else None, "ttft_s": 0.53 if st == "ok" else None, "total_s": 1.5})
    for j, st in enumerate(inc_status):
        t = 100 + 10 * j
        ok = st == "ok"
        rows.append({"t_send": t, "cls": "short", "status": st, "retrieve_s": 0.03 if ok else None,
                     "llm_ttft_s": 0.5 if ok else None, "ttft_s": 0.53 if ok else None, "total_s": 1.5})
    return pd.DataFrame(rows)


def test_tagged_estimator_names_the_retriever_for_an_error_only_fault():
    """s7: 3 % retriever errors => the incident window has a couple of http_503 and no latency change at all."""
    df = _fail_df(["ok", "ok", "http_503", "ok", "ok", "http_503", "ok", "ok"])
    assert rca.localize(df, (20.0, 90.0), (100.0, 190.0))["pred"] is None       # the latency-only estimator cannot see it
    res = rca.localize_tagged(df, (20.0, 90.0), (100.0, 190.0))
    assert res["pred"] == "retriever" and res["evidence"] == "failures" and res["scores"]["retriever"] == 2.0


def test_tagged_estimator_names_the_llm_for_an_outage_and_ignores_client_side_noise():
    df = _fail_df(["llm_error"] * 6 + ["client_error"] * 4)
    assert rca.localize_tagged(df, (20.0, 90.0), (100.0, 200.0))["pred"] == "llm"
    df = _fail_df(["client_error", "client_timeout", "client_drop"] + ["ok"] * 5)   # says nothing about a component
    res = rca.localize_tagged(df, (20.0, 90.0), (100.0, 200.0))
    assert res["evidence"] == "latency" and res["pred"] is None


def test_tagged_estimator_needs_the_failure_RATE_to_rise_and_falls_back_on_ties():
    # one http_503 in a 12-request baseline (8 %) and one in an 8-request incident (12 %): not clearly higher => no vote... but
    # a strictly lower incident rate must never vote
    df = _fail_df(["ok"] * 9 + ["http_503"], base_status="http_503")           # base 1/12 = 8.3 %, incident 1/10 = 10 %
    assert rca.localize_tagged(df, (20.0, 90.0), (100.0, 200.0))["pred"] == "retriever"   # strictly higher: votes
    df = _fail_df(["ok"] * 19 + ["http_503"], base_status="http_503")           # incident 1/20 = 5 % < 8.3 %
    assert rca.localize_tagged(df, (20.0, 90.0), (100.0, 200.0))["evidence"] == "latency"
    tie = _fail_df(["http_503", "llm_error"])
    assert rca.localize_tagged(tie, (20.0, 90.0), (100.0, 200.0))["evidence"] == "latency"  # 1:1 tie => latency evidence decides


# ------------------------------------------------------------------------------------------- run directory fixtures
def make_run(tmp_path, name="s7_x__static1__r1", incident=(200, 320), bad=lambda t: False, status_of=lambda t: "http_503",
             policy="static1", replicas=None, rps=5.0, duration=600, root_cause="retriever"):
    d = tmp_path / name
    d.mkdir()
    rows = []
    for i in range(int(duration * rps)):
        t = (i + 0.5) / rps
        is_bad = bad(t)
        st = status_of(t) if is_bad else "ok"
        ok = st == "ok"
        rows.append({"idx": i, "cls": "short", "t_sched": t, "t_send": t, "t_done": t + 0.5, "weight": 100, "status": st,
                     "ttft_s": 0.5 if ok else None, "total_s": 0.5, "retrieve_s": 0.03 if ok else None,
                     "llm_ttft_s": 0.45 if ok else None, "prompt_tokens": 30 if ok else None, "completion_tokens": 24 if ok else None})
    pd.DataFrame(rows).to_csv(d / "requests.csv", index=False)
    meta = {"phase": "A", "scenario": name.split("__")[0], "policy": policy, "rep": 1, "duration_s": float(duration), "start_epoch": 1000.0,
            "end_epoch": 1000.0 + duration, "valid": True, "invalid_reasons": [], "max_lag_s": 0.01, "n_requests": len(rows),
            "slo": {"target": 0.99, "ttft_s": THR}}
    (d / "meta.json").write_text(json.dumps(meta))
    inc = [{"start": incident[0], "end": incident[1], "kind": "retriever_errors", "root_cause": root_cause}] if incident else []
    (d / "events.json").write_text(json.dumps({"incidents": inc, "fault_events": []}))
    (d / "DONE").write_text("ok\n")
    if replicas:
        (d / "replicas.csv").write_text("epoch,spec,ready\n" + "\n".join(f"{e},{s},{r}" for e, s, r in replicas) + "\n")
    return d


DET = Detector("fast", "request", (Rule(60, 10, 6),), min_events=5)


def test_page_report_labels_before_inside_and_after_and_names_the_trigger(tmp_path):
    bad = lambda t: (200 <= t < 320 and int(t * 5) % 2 == 0) or 100 <= t < 112 or 450 <= t < 456  # noqa: E731
    d = make_run(tmp_path, bad=bad)
    rows = pipeline.page_report(d, [DET], grace_s=60.0, time_scale=1.0)
    verdicts = [(r["verdict"], round(r["start"])) for r in rows]
    inside = [r for r in rows if r["verdict"] == "TRUE"]
    false = [r for r in rows if r["verdict"] == "FALSE"]
    assert len(inside) == 1 and 200 <= inside[0]["start"] <= 320 and inside[0]["where"] == "inside the incident"
    early = [r for r in false if r["start"] < 200]
    late = [r for r in false if r["start"] > 380]
    assert early and "before the incident started" in early[0]["where"], verdicts
    assert late and "outside the 60 s grace period" in late[0]["where"], verdicts
    assert late[0]["bad_before"] and all(s == "http_503" for _, _, s in late[0]["bad_before"])   # the events that fired it
    assert all(t <= late[0]["start"] for t, _, _ in late[0]["bad_before"]) and len(late[0]["bad_before"]) <= 5
    assert not inside[0]["bad_before"]                                            # only FALSE pages carry a trigger list


def test_page_report_grace_decides_true_vs_false_exactly_like_the_scorer(tmp_path):
    bad = lambda t: (200 <= t < 320 and int(t * 5) % 2 == 0) or 450 <= t < 456  # noqa: E731
    d = make_run(tmp_path, bad=bad)
    for grace, expect_late in ((60.0, "FALSE"), (200.0, "TRUE")):             # 450 s is 130 s after the incident ended
        rows = [r for r in pipeline.page_report(d, [DET], grace_s=grace, time_scale=1.0) if r["start"] > 380]
        assert rows and all(r["verdict"] == expect_late for r in rows), (grace, rows)
    # and the verdicts add up to the detection scorer's own counts
    a = pipeline.analyze_run(d, [DET], grace_s=60.0, time_scale=1.0)["detection"][0]
    rows = pipeline.page_report(d, [DET], grace_s=60.0, time_scale=1.0)
    assert a["pages_total"] == len(rows) and a["false_pages"] == sum(r["verdict"] == "FALSE" for r in rows)


def test_page_report_on_a_scenario_without_incident_marks_every_page_false(tmp_path):
    d = make_run(tmp_path, name="s0_baseline__static1__r1", incident=None, bad=lambda t: 100 <= t < 112)
    rows = pipeline.page_report(d, [DET], time_scale=1.0)
    assert rows and all(r["verdict"] == "FALSE" and "no incident" in r["where"] for r in rows)


def test_status_counts_total_and_windowed():
    df = pd.DataFrame({"t_send": [10, 50, 120, 130, 140, 400], "status": ["ok", "ok", "llm_error", "llm_error", "ok", "ok"]})
    assert pipeline.status_counts(df) == {"ok": 4, "llm_error": 2}
    assert pipeline.status_counts(df, 100, 200) == {"llm_error": 2, "ok": 1}
    assert pipeline.status_counts(df, 500, 600) == {}


def test_replica_stats_report_time_at_one_replica_and_first_scale_out(tmp_path):
    # the real run of the user's laptop (s0_baseline under queue), reduced to its change points
    pts = [(-3, 1, 1), (24, 2, 1), (29, 2, 2), (324, 1, 1), (385, 2, 1), (390, 2, 2), (448, 3, 2), (454, 3, 3), (565, 2, 2), (597, 1, 1), (600, 1, 1)]
    rep = []
    for (a, sp, rd), nxt in zip(pts, pts[1:] + [(603, 1, 1)]):
        t = a
        while t < nxt[0]:
            rep.append((1000 + t, sp, rd))
            t += 5
    d = make_run(tmp_path, name="s0_baseline__queue__r1", incident=None, policy="queue", replicas=rep, duration=600, rps=1.0)
    st = pipeline._replica_stats(d, 1000.0, 1600.0)
    assert 1.95 < st["mean_ready_replicas"] < 2.10                         # hand-computed from the screenshot: 2.02
    assert 0.13 < st["frac_time_1_replica"] < 0.19                         # 16 %
    assert st["max_ready_replicas"] == 3 and 20 <= st["first_scale_out_s"] <= 30


def test_static_runs_report_one_replica_all_the_time(tmp_path):
    rep = [(1000 + 5 * i, 1, 1) for i in range(40)]
    d = make_run(tmp_path, name="s0_baseline__static1__r1", incident=None, replicas=rep, duration=190, rps=1.0)
    st = pipeline._replica_stats(d, 1000.0, 1190.0)
    assert st["frac_time_1_replica"] == pytest.approx(1.0) and st["first_scale_out_s"] is None and st["max_ready_replicas"] == 1


# ------------------------------------------------------------------------------------------- summarize output
def test_summarize_shows_outcomes_estimators_autoscaler_and_pages(tmp_path, capsys):
    bad = lambda t: 200 <= t < 320 and int(t * 5) % 2 == 0  # noqa: E731
    d = make_run(tmp_path, bad=bad, policy="queue",
                 replicas=[(1000 + 5 * i, 1 if i < 50 else 2, 1) for i in range(120)], rps=2.0)
    assert cli.main(["summarize", str(d), "--pages"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"request outcomes \(all 1200\): .*http_503 \d+", out)
    assert re.search(r"outcomes of the requests sent in the fault window: .*http_503", out)
    assert "failure-tagged estimator: retriever (evidence: failures)" in out
    assert "class-adjusted latency estimator:" in out
    assert re.search(r"autoscaler: mean ready replicas 1\.\d+; exactly 1 replica for \d+% of the run; max 1; first scale-out at \+2\d\d s", out)
    assert "pages (a page is TRUE only if it STARTS inside [incident start, incident end + 60 s]" in out
    assert re.search(r"\s(TRUE|FALSE)\s", out)


def test_summarize_without_pages_flag_does_not_list_pages(tmp_path, capsys):
    d = make_run(tmp_path, bad=lambda t: 200 <= t < 320 and int(t * 5) % 2 == 0)
    assert cli.main(["summarize", str(d)]) == 0
    assert "pages (a page is TRUE" not in capsys.readouterr().out


# ------------------------------------------------------------------------------------------- report
def test_report_tables_carry_the_three_estimators_and_the_grace_sensitivity(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    (runs / "A").mkdir(parents=True)
    make_run(runs / "A", name="s7_slow_burn__static1__r1", bad=lambda t: 200 <= t < 320 and int(t * 5) % 2 == 0, rps=2.0)
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    a = pipeline.analyze_phase("A", grace_s=60.0)
    rc = report.aggregate_rca(a["rca"])
    assert {"attribution_acc", "class_adjusted_acc", "failure_tagged_acc"} <= set(rc.columns)
    row = rc.iloc[0]
    assert row["attribution_acc"] == 0.0 and row["failure_tagged_acc"] == 1.0
    gs = report.grace_sensitivity("A")
    assert set(gs["grace_s"]) == set(report.GRACES_S) and {"detect_rate", "false_pages_per_hr", "false_pages_total"} <= set(gs.columns)
    assert (gs.groupby("detector")["false_pages_total"].apply(lambda s: list(s) == sorted(s, reverse=True))).all()  # a longer grace never ADDS false pages


# ------------------------------------------------------------------------------------------- autoscaling overlay
def write(tmp_path, text, name="autoscaling.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_overlay_missing_file_means_chart_defaults(tmp_path):
    assert k8s.autoscaling_overlay(tmp_path / "nope.yaml") is None


def test_overlay_accepts_trigger_keys_and_rejects_everything_else(tmp_path):
    ok = write(tmp_path, "autoscaling:\n  queue: {threshold: 4}\n  cpu: {utilization: 150}\n  maxReplicas: 3\n")
    assert k8s.autoscaling_overlay(ok) == {"queue": {"threshold": 4}, "cpu": {"utilization": 150}, "maxReplicas": 3}
    with pytest.raises(ValueError, match="chosen per run"):
        k8s.autoscaling_overlay(write(tmp_path, "autoscaling:\n  policy: queue\n", "a.yaml"))
    with pytest.raises(ValueError, match="unknown autoscaling key"):
        k8s.autoscaling_overlay(write(tmp_path, "autoscaling:\n  queeu: {threshold: 4}\n", "b.yaml"))
    with pytest.raises(ValueError, match="only top-level key"):
        k8s.autoscaling_overlay(write(tmp_path, "slo:\n  target: 0.5\n", "c.yaml"))       # must not be a way to change the SLO
    with pytest.raises(ValueError, match="must be a mapping"):
        k8s.autoscaling_overlay(write(tmp_path, "autoscaling:\n  queue: 4\n", "d.yaml"))
    assert k8s.autoscaling_overlay(write(tmp_path, "# only comments\n", "e.yaml")) == {}


def test_values_file_args_order_and_optionality(tmp_path, monkeypatch):
    monkeypatch.setattr(k8s, "AUTOSCALING_FILE", tmp_path / "autoscaling.yaml")
    assert k8s.values_file_args()[:2] == ["-f", str(cfg.CONFIG_DIR / "slo.yaml")]
    assert str(tmp_path / "autoscaling.yaml") not in k8s.values_file_args()
    write(tmp_path, "autoscaling:\n  queue: {threshold: 4}\n")
    args = k8s.values_file_args()
    files = [a for a in args if a != "-f"]
    assert files.index(str(tmp_path / "autoscaling.yaml")) == 1 and files[0].endswith("slo.yaml")
    write(tmp_path, "autoscaling:\n  policy: burn\n")        # a bad file must stop the deploy BEFORE helm runs
    with pytest.raises(ValueError):
        k8s.values_file_args()


def test_helm_upgrade_full_passes_the_overlay_to_helm(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(k8s, "helm", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(k8s, "AUTOSCALING_FILE", write(tmp_path, "autoscaling:\n  burn: {threshold: 6}\n"))
    k8s.helm_upgrade_full()
    cmd = list(calls[0])
    assert "--reset-values" in cmd and str(tmp_path / "autoscaling.yaml") in cmd
    assert cmd.index(str(tmp_path / "autoscaling.yaml")) > cmd.index(str(cfg.CONFIG_DIR / "slo.yaml"))


def test_example_overlay_is_valid_and_equals_the_chart_defaults():
    """Copying the example unchanged must not change the deployment (so `make autoscaling` is always safe to run first)."""
    ex = k8s.autoscaling_overlay(ROOT / "config" / "autoscaling.yaml.example")
    chart = yaml.safe_load(read("charts/slo-loop-app/values.yaml"))["autoscaling"]
    for trig in ("cpu", "queue", "burn"):
        assert ex[trig] == chart[trig], trig


def test_the_overlay_really_changes_the_rendered_scaledobject(tmp_path):
    import shutil
    if shutil.which("go") is None:
        pytest.skip("Go toolchain needed for the offline chart renderer")
    import test_chart as tc
    renderer = tc.renderer.__wrapped__(tmp_path_factory=type("F", (), {"mktemp": staticmethod(lambda n: tmp_path)})) if hasattr(tc.renderer, "__wrapped__") else None
    if renderer is None:
        pytest.skip("renderer fixture not callable outside pytest")
    ov = {"autoscaling": {"queue": {"threshold": 4}, "burn": {"threshold": 6}, "cpu": {"utilization": 150}}}
    for pol, expect in (("queue", "4"), ("burn", "6"), ("cpu", "150")):
        so = tc.by_kind(tc.docs(renderer, tc.load_values(pol, ov), tmp_path), "ScaledObject")["llm"]
        trig = so["spec"]["triggers"][0]
        assert str(trig["metadata"].get("threshold", trig["metadata"].get("value"))) == expect, pol


def test_deploy_script_and_verify_script_know_the_overlay_and_the_calibration_state():
    deploy, verify = read("scripts/06-deploy-app.sh"), read("scripts/verify.sh")
    assert 'config/autoscaling.yaml' in deploy and "autoscaling_overlay()" in deploy
    assert deploy.index("autoscaling.yaml") < deploy.index("images.yaml")      # precedence: slo < autoscaling < images
    assert 'OVERLAY=(-f "$ROOT/config/autoscaling.yaml")' in verify and '"${OVERLAY[@]}"' in verify
    assert "calibrated: true" in verify and "do NOT re-run them" in verify and "Next: make latency && make capacity && make calibrate" in verify


def test_make_targets_exist():
    mk = read("Makefile")
    for t in ("autoscaling", "simulate", "share"):
        assert re.search(rf"^{t}:", mk, re.M) and f"$(LAB) {t}" in mk and t in mk.split("\n")[7]


def test_autoscaling_command_prints_effective_values(monkeypatch, capsys):
    monkeypatch.setattr(k8s, "ensure_release_deployed", lambda: None)
    monkeypatch.setattr(k8s, "helm_upgrade_full", lambda *a, **k: None)
    monkeypatch.setattr(k8s, "effective_autoscaling", lambda: {"queue": {"threshold": 4}, "cpu": {"utilization": 60}, "maxReplicas": 3})
    assert cli.main(["autoscaling"]) == 0
    out = capsys.readouterr().out
    assert "effective autoscaling values" in out and "'threshold': 4" in out and "--scenario s0_baseline --policy cpu" in out


# ------------------------------------------------------------------------------------------- scenario wording
def test_s6_description_no_longer_claims_a_pure_latency_fault():
    sc = cfg.load_scenario("s6_netem_delay")
    assert "readiness probe" in sc["description"] and "OUTAGE" in sc["description"]
    assert sc["incidents"][0]["root_cause"] == "llm"


# ------------------------------------------------------------------------------------------- trigger simulator
def test_simulation_is_deterministic_and_static_never_scales():
    p = sim.Params(policy="queue", seed=5)
    a, b = sim.simulate(p), sim.simulate(p)
    assert a.events == b.events and np.array_equal(a.ready, b.ready)
    s = sim.simulate(sim.Params(policy="static1", seed=5))
    assert s.ready.max() == 1 and s.first_scale_out() is None


def test_simulated_baseline_is_calibrated_to_about_one_percent_bad():
    p = sim.Params()
    thr = sim.calibrate_bad_after(p, duration_s=12000.0)
    r = sim.simulate(sim.Params(policy="static1", duration_s=12000.0, bad_after_s=thr, noise=0.0, seed=3))
    assert 0.003 < float((r.sojourns > thr).mean()) < 0.02


def test_queue_trigger_scales_on_a_burst_and_a_higher_threshold_is_calmer_at_baseline():
    burst = dict(burst_start_s=200.0, burst_end_s=320.0)
    lo = sim.monte_carlo(sim.Params(policy="queue", threshold=2, **burst), reps=25)
    assert lo["runs_that_ever_scale_out"] == 1.0 and lo["scale_out_after_burst_s_median"] <= 60
    calm = sim.monte_carlo(sim.Params(policy="queue", threshold=2), reps=25)
    calmer = sim.monte_carlo(sim.Params(policy="queue", threshold=6), reps=25)
    assert calmer["mean_ready_replicas"] < calm["mean_ready_replicas"]
    assert calmer["frac_time_at_1_replica"] > calm["frac_time_at_1_replica"]


def test_cpu_trigger_with_a_request_smaller_than_what_the_pod_burns_overscales_at_baseline():
    """The finding the simulator exists to show: 60 % of a 1000 m request, with llama.cpp burning ~1.8 cores while busy."""
    cpu = sim.monte_carlo(sim.Params(policy="cpu", cpu_util_target=60.0, base_rps=0.178), reps=15)
    assert cpu["runs_that_ever_scale_out"] == 1.0 and cpu["mean_ready_replicas"] > 2.0
    relaxed = sim.monte_carlo(sim.Params(policy="cpu", cpu_util_target=150.0, base_rps=0.178), reps=15)
    assert relaxed["mean_ready_replicas"] < cpu["mean_ready_replicas"]


def test_a_single_bad_request_is_a_huge_burn_rate_at_this_traffic_level():
    """Why the burn trigger is noise-dominated here: ~5 requests finish per 30 s, so ONE bad one reads as burn >= 20."""
    window_requests = 0.178 * 30
    assert (1 / window_requests) / (1 - 0.99) > 15


def test_simulate_command_prints_a_table_for_each_policy(capsys):
    assert cli.main(["simulate", "--rps", "0.178", "--reps", "6"]) == 0
    out = capsys.readouterr().out
    for pol in ("queue", "burn", "cpu"):
        assert re.search(rf"^{pol}\s+\S+\s+baseline", out, re.M) and re.search(rf"^{pol}\s+\S+\s+3x burst", out, re.M)
    assert "NOT a measurement" in out
