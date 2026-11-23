"""`slo-lab` command line. Every make target that touches Python goes through here."""
from __future__ import annotations

import argparse
import os
import random
import sys

from . import config as cfg
from . import k8s


def _cmd_latency(a) -> int:
    from . import capacity
    capacity.measure_latency(per_class=a.per_class)
    return 0


def _cmd_profile(a) -> int:
    cur = cfg.load_load().get("profile", "?")
    profiles = cfg.load_profiles()
    if not a.name:
        for n, p in profiles.items():
            print(f"{'*' if n == cur else ' '} {n:9s} {p['description']}")
        print("\nSwitch with: make profile P=<name>   (then: make latency && make capacity && make calibrate)")
        return 0
    if a.name == cur:
        print(f"profile '{cur}' is already active; nothing changed.")
        return 0
    cfg.apply_profile(a.name)
    print(f"profile switched: {cur} -> {a.name}. Capacity and SLO thresholds are now stale.")
    print("Next: make latency && make capacity && make calibrate")
    return 0


def _cmd_capacity(a) -> int:
    from . import capacity
    capacity.probe_capacity(step_s=a.step_s)
    return 0


def _cmd_set_base(a) -> int:
    from . import capacity
    capacity.set_base_rps(a.frac)
    return 0


def _cmd_calibrate(a) -> int:
    from . import capacity
    capacity.calibrate_slo(duration_s=a.duration)
    return 0


def _cmd_run(a) -> int:
    from . import runner
    runner.run_one(a.phase, a.scenario, a.policy, a.rep, force=a.force, duration_override=a.duration)
    return 0


def _plan(phase: str, reps: int, only: list[str] | None) -> list[tuple[str, str, int]]:
    m = cfg.load_matrix()[phase]
    jobs = [(sc, pol, r) for sc in m["scenarios"] for pol in m["policies"] for r in range(1, reps + 1)]
    if only:
        jobs = [j for j in jobs if j[0] in only]
    random.Random(int(m["seed"])).shuffle(jobs)
    return jobs


MAX_CONSECUTIVE_FAILED_JOBS = 2


def _log_failure(msg: str) -> None:
    import time
    log = cfg.RESULTS_DIR / "matrix-failures.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def _run_with_repair(runner, phase: str, sc: str, pol: str, rep: int) -> str:
    """-> 'ok' | 'skipped' (scenario cannot work with this deployment) | 'failed' (after one repair + one retry)."""
    name = f"{sc}__{pol}__r{rep}"
    for attempt in (1, 2):
        try:
            runner.run_one(phase, sc, pol, rep)
            return "ok"
        except k8s.ScenarioNotRunnable as e:
            print(f"\n[matrix] SKIPPED {name}: {e.args[0] if e.args else e}", file=sys.stderr)
            _log_failure(f"skipped {name}: {e}")
            return "skipped"
        except Exception as e:  # noqa: BLE001  (a long unattended phase must survive one bad run)
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            print(f"\n[matrix] {name}: attempt {attempt} FAILED: {first}", file=sys.stderr)
            _log_failure(f"{name} attempt {attempt} failed: {e}")
            if attempt == 2:
                return "failed"
            print("[matrix] repairing the lab, then retrying this run once ...")
            try:
                k8s.recover()
            except Exception as e2:  # noqa: BLE001
                print(f"[matrix] automatic repair failed: {e2}", file=sys.stderr)
                _log_failure(f"{name} recovery failed: {e2}")
                return "failed"
    return "failed"


def _cmd_matrix(a) -> int:
    """Run a phase unattended. A run that fails on INFRASTRUCTURE (Helm release left 'failed', stale model mount, pod that
    never became ready) is repaired once (`k8s.recover`, same as `make recover`) and retried once; a scenario that cannot work
    with the current deployment is skipped with the reason; two jobs in a row that still fail abort the phase (something is
    really broken and the next 10 would fail too). Finished runs are always skipped, so re-running resumes."""
    from . import runner
    jobs = _plan(a.phase, a.reps, a.only)
    print(f"[matrix] phase {a.phase}: {len(jobs)} runs (~{len(jobs) * 12} min). Resumable: finished runs are skipped.")
    problems: list[str] = []
    streak = 0
    for sc, pol, rep in jobs:
        if a.dry_run:
            print(f"  {a.phase} {sc} {pol} r{rep}")
            continue
        outcome = _run_with_repair(runner, a.phase, sc, pol, rep)
        name = f"{sc}__{pol}__r{rep}"
        if outcome == "ok":
            streak = 0
        elif outcome == "skipped":
            problems.append(f"{name} (not runnable, see message above)")
            streak = 0
        else:
            problems.append(f"{name} (failed)")
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILED_JOBS:
                print(f"\n[matrix] ABORTED: {streak} runs in a row failed even after repair. Run `make diag`, paste "
                      "results/diag-*.txt, then `make recover && make verify` and re-run the same command (finished runs are kept).",
                      file=sys.stderr)
                break
    if problems:
        print(f"\n[matrix] {len(problems)} job(s) did not finish: {'; '.join(problems)}.\nDetails: {cfg.RESULTS_DIR / 'matrix-failures.log'}. "
              "Fix the cause, then re-run the same command: only the missing runs execute.", file=sys.stderr)
        return 1
    return 0


def _cmd_recover(_a) -> int:
    k8s.recover()
    print("\nNext: make verify")
    return 0


def _cmd_analyze(a) -> int:
    from .analysis import report
    print(report.build_report(include_invalid=a.include_invalid))
    return 0


def _cmd_robust(a) -> int:
    """Paired statistics + with/without-accepted-runs tables for the finished data -> results/ROBUSTNESS.md and robust_*.csv."""
    from .analysis import robust
    robust.build_robust(include_invalid=a.include_invalid)
    out = cfg.RUNS_DIR.parent
    print((out / robust.DIGEST_NAME).read_text())
    print(f"[robust] full report: {out / 'ROBUSTNESS.md'}   tables: {out}/robust_*.csv   (make share packs all of it)")
    return 0


def _cmd_summarize(a) -> int:
    """Print per-class latency and every detector's verdict for ONE run directory."""
    import json
    from pathlib import Path

    from .analysis.detect import detector_from_dict
    from .analysis.pipeline import analyze_run, page_report, replica_timeline, status_counts
    from .analysis import slo as slo_mod

    run = Path(a.run)
    if not (run / "meta.json").exists():
        print(f"not a run directory: {run}", file=sys.stderr)
        return 2
    meta = json.loads((run / "meta.json").read_text())
    res = analyze_run(run, [detector_from_dict(d) for d in cfg.load_detectors()], time_scale="auto")
    df = slo_mod.annotate(slo_mod.load_requests(run / "requests.csv"), meta["slo"]["ttft_s"])
    print(f"run: {run.name}   valid={meta['valid']}   requests={meta['n_requests']}   max client lag={meta['max_lag_s']:.3f}s")
    if meta["invalid_reasons"]:
        print("notes:", "; ".join(meta["invalid_reasons"]))
    print("\nclass     n   ttft_p50  ttft_p95  error%  good%")
    for name, s in slo_mod.class_stats(df).items():
        f = lambda v, fmt: "   n/a" if v is None else fmt.format(v)  # noqa: E731
        print(f"{name:8s} {s['n']:4d}  {f(s['ttft_p50'], '{:7.2f}s')}  {f(s['ttft_p95'], '{:7.2f}s')}  "
              f"{f(None if s['error_ratio'] is None else 100 * s['error_ratio'], '{:5.1f}')}  "
              f"{f(None if s['good_ratio'] is None else 100 * s['good_ratio'], '{:5.1f}')}")
    if res["summary"]["time_scale"] != 1.0:
        print(f"\n(this run has only {len(df)} requests, so each detector's windows were stretched until its shortest window "
              "holds enough events; `x` column. See docs/DESIGN.md, 'low-traffic alerting')")
    print("\ndetector                  detected  ttd_s  false_pages  pages  x  rules")
    for d in res["detection"]:
        ttd = "-" if d["ttd_s"] is None else f"{d['ttd_s']:.0f}"
        print(f"{d['detector']:25s} {str(d['detected']):8s}  {ttd:>5s}  {d['false_pages']:11d}  {d['pages_total']:5d}  "
              f"{d['time_scale']:g}  {d['rules_evaluated']}/{d['rules_evaluated'] + d['rules_dropped']}")
    events = json.loads((run / "events.json").read_text())
    fmt_counts = lambda d: ", ".join(f"{k} {v}" for k, v in d.items()) or "-"  # noqa: E731
    print(f"\nrequest outcomes (all {len(df)}): {fmt_counts(status_counts(df))}")
    for inc in events.get("incidents", []):
        win = (df["t_send"] >= float(inc["start"])) & (df["t_send"] < float(inc["end"]))
        before = (df["t_send"] >= 20.0) & (df["t_send"] < float(inc["start"]) - 10.0)
        if win.sum() and before.sum():
            bad_in, bad_before = float((~df.loc[win, "good"]).mean()), float((~df.loc[before, "good"]).mean())
            print(f"\nfault effect ({inc.get('kind', '?')}, t={float(inc['start']):.0f}-{float(inc['end']):.0f} s): {100 * bad_in:.0f}% of the "
                  f"{int(win.sum())} requests sent in the window were BAD, versus {100 * bad_before:.0f}% of the {int(before.sum())} before it")
            print(f"  outcomes of the requests sent in the fault window: {fmt_counts(status_counts(df, float(inc['start']), float(inc['end'])))}")
            if bad_in < bad_before + 0.10:
                print("  -> WARNING: no visible effect on the SLI. The run is valid but this fault may not have done what the scenario intends.")
    for r in res["rca"]:
        pred = "none (abstained)" if r["pred"] is None else r["pred"]
        sc = ", ".join(f"{k}={v:.2f}s" for k, v in (r.get("scores") or {}).items())
        print(f"\nroot-cause attribution: truth={r['truth']}  predicted={pred}  (baseline n={r['n_base']}, incident n={r['n_inc']} usable, "
              f"{r['n_inc_failed']} failed)" + (f"\n  stage-delta scores: {sc}" if sc else ""))
        adj = "none (abstained)" if r.get("pred_adjusted") is None else r["pred_adjusted"]
        tag = "none (abstained)" if r.get("pred_tagged") is None else r["pred_tagged"]
        print(f"  class-adjusted latency estimator: {adj}   |   failure-tagged estimator: {tag} (evidence: {r.get('evidence_tagged') or '-'})")
    sm = res["summary"]
    if sm.get("mean_ready_replicas") is not None:
        so = sm.get("first_scale_out_s")
        print(f"\nautoscaler: mean ready replicas {sm['mean_ready_replicas']:.2f}; exactly 1 replica for {100 * sm['frac_time_1_replica']:.0f}% of the run; "
              f"max {sm['max_ready_replicas']:.0f}; first scale-out " + ("never" if so is None else f"at +{so:.0f} s"))
    tl = replica_timeline(run, float(meta["start_epoch"]))
    if tl:
        print("\nllm replicas over the run (seconds since start: desired/ready):")
        print("  " + "   ".join(f"{t:+.0f}s {spec}/{ready}" for t, spec, ready in tl[:24]) + ("   ..." if len(tl) > 24 else ""))
    if a.pages:
        grace = float(a.grace)
        pages = page_report(run, [detector_from_dict(d) for d in cfg.load_detectors()], grace_s=grace, time_scale="auto")
        print(f"\npages (a page is TRUE only if it STARTS inside [incident start, incident end + {grace:.0f} s]; times are seconds since the run started):")
        if not pages:
            print("  no detector paged in this run")
        for pg in pages:
            print(f"  {pg['detector']:22s} {pg['start']:6.0f}s -> {pg['end']:6.0f}s  {pg['verdict']:5s}  {pg['where']}")
            if pg["bad_before"]:
                print("      bad requests that completed in the 120 s before it: " + ", ".join(f"{t:.0f}s {c}/{st}" for t, c, st in pg["bad_before"]))
    return 0


def _cmd_autoscaling(_a) -> int:
    k8s.ensure_release_deployed()
    ov = k8s.autoscaling_overlay()
    if ov is None:
        print("config/autoscaling.yaml does not exist: the chart defaults are in use. To change thresholds:\n"
              "  cp config/autoscaling.yaml.example config/autoscaling.yaml && nano config/autoscaling.yaml && make autoscaling")
    else:
        print(f"applying config/autoscaling.yaml: {ov}")
    k8s.helm_upgrade_full()
    eff = k8s.effective_autoscaling()
    print("effective autoscaling values of the deployed release:")
    for k_ in ("cpu", "queue", "burn", "minReplicas", "maxReplicas", "scaleUpStabilizationSeconds", "scaleDownStabilizationSeconds"):
        print(f"  {k_:30s} {eff.get(k_)}")
    print("\nNext: re-run a baseline per policy to check them, e.g.\n"
          "  .venv/bin/slo-lab run --phase X --scenario s0_baseline --policy cpu --rep 1 --force")
    return 0


def _autoscaling_defaults() -> dict:
    """Trigger thresholds the next deploy would use: chart defaults, overridden by config/autoscaling.yaml."""
    chart = cfg.load_yaml(cfg.CHART_DIR / "values.yaml")["autoscaling"]
    ov = k8s.autoscaling_overlay() or {}
    return {t: {**chart.get(t, {}), **ov.get(t, {})} for t in ("cpu", "queue", "burn")}


def _cmd_simulate(a) -> int:
    from . import sim
    load = cfg.load_load()
    rps = a.rps or float(load.get("base_rps") or 0.178)
    svc = a.service_s or 2.93
    th = _autoscaling_defaults()
    cur = {"queue": th["queue"].get("threshold", 2), "burn": th["burn"].get("threshold", 2), "cpu": th["cpu"].get("utilization", 60)}
    base = sim.Params(service_s=svc, base_rps=rps)
    bad_after = sim.calibrate_bad_after(base)
    print(f"model: base {rps:.3f} req/s, solo service {svc:.2f} s (rho = {rps * svc:.2f}), a request is BAD above {bad_after:.1f} s of sojourn "
          f"(= the p99 of a clean single-replica baseline, like `make calibrate`), {a.reps} simulated 600 s runs per row.")
    print("This is a prediction from a deliberately simple queue model, NOT a measurement; see lab/sim.py for every assumption.\n")
    print(f"{'policy':8s} {'threshold':>9s} {'scenario':9s} {'mean ready':>10s} {'1 replica':>9s} {'3 replicas':>10s} {'ever scales':>11s} {'first scale-out':>16s}")
    for pol in a.policies:
        thr = a.threshold if a.threshold is not None else cur[pol]
        for name, burst in (("baseline", {}), ("3x burst", {"burst_start_s": 200.0, "burst_end_s": 320.0})):
            kw = dict(policy=pol, service_s=svc, base_rps=rps, bad_after_s=bad_after, noise=0.0, burn_window_s=a.burn_window_s, **burst)
            if pol == "cpu":
                kw["cpu_util_target"] = float(thr)
            else:
                kw["threshold"] = float(thr)
            r = sim.monte_carlo(sim.Params(**kw), reps=a.reps)
            so = r.get("scale_out_after_burst_s_median", r.get("scale_out_after_start_s_median"))
            print(f"{pol:8s} {thr:9g} {name:9s} {r['mean_ready_replicas']:10.2f} {100 * r['frac_time_at_1_replica']:8.0f}% {100 * r['frac_time_at_3_replicas']:9.0f}% "
                  f"{100 * r['runs_that_ever_scale_out']:10.0f}% {('never' if so is None else f'{so:.0f} s'):>16s}")
    print("\nHow to read it: for a fair policy comparison the BASELINE rows should stay near 1 replica (the autoscaler must not scale on plain "
          "load) while the burst rows scale out quickly. See docs/AUTOSCALING.md.")
    return 0


def _cmd_share(a) -> int:
    """Pack results + measured config into one zip (and copy it to the Windows Downloads folder when that is unambiguous)."""
    import shutil
    import time
    from pathlib import Path

    from . import share
    if not (cfg.RUNS_DIR.exists() and any(cfg.RUNS_DIR.rglob("DONE"))):
        print("[share] there are no finished runs under results/runs yet; the zip would only hold configuration.", file=sys.stderr)
    out = Path(a.out) if a.out else cfg.ROOT / f"results-share-{time.strftime('%Y%m%d-%H%M')}.zip"
    info = share.build_share(cfg.ROOT, cfg.RESULTS_DIR, cfg.CONFIG_DIR, out, max_file_mb=a.max_file_mb)
    print(f"[share] {info['path']}  ({info['n_files']} files, {info['bytes'] / 1024 / 1024:.1f} MB)")
    print("[share] finished runs per phase: " + (", ".join(f"{k}: {v}" for k, v in info["inventory"].items()) or "none"))
    for name, mb in info["skipped"]:
        print(f"[share] left out (larger than {a.max_file_mb:g} MB): {name} ({mb} MB)")
    dl = share.windows_downloads()
    if dl is not None and not a.no_copy:
        try:
            shutil.copy2(info["path"], dl / info["path"].name)
            print(f"[share] also copied to your Windows Downloads folder: {dl / info['path'].name}")
        except OSError as e:
            print(f"[share] could not copy to {dl}: {e}", file=sys.stderr)
    elif dl is None and not a.no_copy:
        cands = share.windows_download_candidates()
        if cands:
            me = os.environ.get("USER", "").lower()
            print("[share] more than one Windows profile has a Downloads folder, so I will not guess. Paste the ONE line for your profile (nothing to edit):")
            for d in cands:
                print(f'  cp "{info["path"]}" "{d}/"' + ("     # same name as your Linux user" if d.parent.name.lower() == me else ""))
        else:
            print("[share] no Windows Downloads folder found under /mnt/c/Users; use the Explorer path below.")
    unc = share.wsl_unc_path(info["path"])
    if unc:
        print(f"[share] in Windows Explorer you can also open: {unc}")
    print("Attach this one file to the chat; nothing in it is a credential (see the docstring of lab/share.py).")
    return 0


def _cmd_health(a) -> int:
    """Per-run host-health table: which finished runs were measured on a slow host? With --rerun: re-run the flagged ones."""
    from . import gate

    if a.rerun and a.accept:
        print("use either --rerun or --accept, not both", file=sys.stderr)
        return 2
    if a.rerun and a.phase in (None, "G"):
        print("--rerun needs --phase A, B or X (gate runs are not re-run)", file=sys.stderr)
        return 2
    if a.accept and a.phase in (None, "G"):
        print("--accept needs --phase A, B or X (gate runs are not accepted)", file=sys.stderr)
        return 2
    rows = gate.health_rows(cfg.RUNS_DIR, a.phase)
    if not rows:
        print(f"no finished runs under {cfg.RUNS_DIR}" + (f"/{a.phase}" if a.phase else ""))
        return 0
    print(gate.format_health(rows, hostlog_present=any(r.get("busy") is not None for r in rows)))
    if a.accept:
        return _accept_flagged(a, rows)
    if not a.rerun:
        return 0
    flagged = [r for r in rows if r["flags"]]
    # a run that was already measured twice and is flagged again is reproducible (same traffic, healthy host): re-running it a third time
    # costs 11 minutes and shows the same thing. INVALID is different (a precondition failed), so it is always re-run.
    stuck = [r for r in flagged if r.get("attempts", 1) >= 2 and "INVALID" not in r["flags"]]
    bad = [r for r in flagged if r not in stuck]
    if stuck:
        print(f"\n[rerun] SKIPPING {len(stuck)} run(s) already measured 2x or more and still flagged (reproducible, re-running will not change it): "
              + ", ".join(f"{r['name']} [{','.join(r['flags'])}]" for r in stuck)
              + f"\n        Keep them with: make gate && make accept-flagged PHASE={a.phase}")
    if not bad:
        if not stuck:
            print("\n[rerun] nothing to re-run: no run is flagged BAD, SLOW or INVALID.")
        else:
            print("\n[rerun] nothing left to re-run.")
        return 0
    if not a.no_gate_check:
        ok, msg = gate.recent_gate_ok(cfg.RUNS_DIR)
        if not ok:
            print(f"\n[rerun] REFUSED: {msg}. Re-running on a slow machine only makes more bad data.", file=sys.stderr)
            return 1
        print(f"\n[rerun] {msg}")
    from . import runner

    print(f"[rerun] re-running {len(bad)} flagged run(s) (about {len(bad) * 12} min): " + ", ".join(f"{r['name']} [{','.join(r['flags'])}]" for r in bad))
    for r in bad:
        try:
            runner.run_one(r["phase"], r["scenario"], r["policy"], int(r["rep"]), force=True)
        except Exception as e:  # noqa: BLE001 - report which run failed and stop; finished re-runs are kept
            print(f"\n[rerun] {r['name']} FAILED: {e}\nFix the cause (make diag / make recover), then run the same command again.", file=sys.stderr)
            return 1
    print("\n[rerun] done. Next: make gate, then make health PHASE=" + a.phase + " (every row should now show '-' in flags).")
    return 0


def _accept_flagged(a, rows) -> int:
    """Keep runs that are STILL flagged BAD/SLOW after a re-run on a healthy machine: same traffic + healthy host + same flag = reproducible."""
    from . import gate

    flagged = [r for r in rows if r["flags"]]
    if not flagged:
        print("\n[accept] nothing to accept: no run is flagged BAD, SLOW or INVALID.")
        return 0
    if not a.no_gate_check:
        ok, msg = gate.recent_gate_ok(cfg.RUNS_DIR)
        if not ok:
            print(f"\n[accept] REFUSED: {msg}. A flag may only be accepted when the machine is known to be healthy right now.", file=sys.stderr)
            return 1
        print(f"\n[accept] {msg}")
    note = f"accepted after re-run on a healthy machine (phase {a.phase})"
    take, refused = gate.accept_flagged(cfg.RUNS_DIR.parent / gate.ACCEPT_NAME, flagged, note)
    if take:
        print(f"[accept] kept {len(take)} run(s) that are flagged but reproducible (same traffic, same flag, healthy host). They stay in the analysis:")
        for r in take:
            sp = "n/a" if r["short_p50"] is None else f"{r['short_p50']:.2f} s"
            print(f"    {r['name']:34s} [{','.join(r['flags'])}]  short p50 {sp}, steady good {r['pre_good_pct']:.1f}% of {r['pre_n']}, measured {r.get('attempts', 1)}x")
        if any(r.get("attempts", 1) < 2 for r in take):
            print("    note: 'measured 1x' = never re-run, or re-run before v2.5.9 (which did not record it). Accept only runs you did re-run.")
        print(f"[accept] recorded in {cfg.RUNS_DIR.parent / gate.ACCEPT_NAME}; `make health` now shows them as accepted. Report them as 'flagged, reproducible' and run the analysis with and without them.")
    if refused:
        print("[accept] NOT accepted (INVALID: a precondition was violated, they must be re-run): " + ", ".join(r["name"] for r in refused), file=sys.stderr)
        return 0 if take else 1
    return 0


def _cmd_gate(a) -> int:
    """Health gate: one idle s0_baseline run at a fixed single replica. Exit 0 = healthy, 1 = not healthy (chain long runs behind it)."""
    from . import gate, runner
    if a.history:
        print(gate.format_history(gate.history(cfg.RUNS_DIR), a.min_good))
        return 0
    node = f"{cfg.KUBE_CONTEXT.removeprefix('kind-')}-control-plane"
    running = gate.running_containers()
    if running is None:
        print("[gate] Docker is not reachable from this Ubuntu. Start Docker Desktop, wait for 'Engine running', and check "
              "Settings > Resources > WSL integration > Ubuntu. (Your cluster and results are safe; this is not a lost cluster.)", file=sys.stderr)
        return 1
    stray = gate.stray_containers(running, node)
    if stray:
        print(f"[gate] WARNING: other containers are running and share your CPU: {', '.join(stray)}")
        print(f"[gate] Stop them for the experiments (nothing is deleted):  docker stop {' '.join(stray)}")
        print(f"[gate] and keep them from starting again after the next Docker restart:  docker update --restart=no {' '.join(stray)}")
    rep = gate.next_rep(cfg.RUNS_DIR)
    out = runner.run_one(gate.GATE_PHASE, gate.GATE_SCENARIO, gate.GATE_POLICY, rep, force=True)
    verdict = gate.evaluate(out, a.min_good)
    print("\n" + gate.format_verdict(verdict, f"{gate.GATE_PHASE}/{out.name}", a.min_good))
    if stray:
        print(f"(the warning above still applies: {', '.join(stray)} were running during this gate)")
    return 0 if verdict["passed"] else 1


def _cmd_status(_a) -> int:
    from . import status
    return status.main()


DEMO_SCENARIO = "demo_incident"


def _cmd_dashboards(a) -> int:
    from . import dashboards
    return dashboards.main(open_browser=a.open)


def _cmd_annotate(a) -> int:
    from . import annotate
    try:
        i = annotate.post_now(a.text, a.tag)
    except Exception as e:  # noqa: BLE001
        print(f"[annotate] could not add the marker at {cfg.GRAFANA_URL}: {e}. Is the lab up? Run: make status", file=sys.stderr)
        return 1
    print(f"[annotate] marker {i} added ({a.text!r}, tags: slo-loop{''.join(', ' + t for t in a.tag)}). It shows on every dashboard time series that has the tag filter.")
    return 0


def demo_timeline(scenario: dict) -> str:
    """What the person watching the dashboards should expect, derived from the scenario file (so it cannot drift from it)."""
    f = scenario["faults"][0]
    at, until, total = float(f["at"]), float(f["until"]), float(scenario["duration_s"])

    def mmss(x: float) -> str:
        return f"{int(x) // 60}:{int(x) % 60:02d}"
    return "\n".join([
        f"  +{mmss(0)}   the clock starts when the harness prints 'measuring {total:.0f}s' (before that it switches the policy and warms up, about 2 minutes)",
        f"  +{mmss(at)}   FAULT: the retriever starts adding latency (red band on every time series, named on hover)",
        f"  +{mmss(at + 30)}   requests that were delayed finish late: burn rate climbs; SLOBurnFast goes pending, then firing (yellow marker)",
        f"  +{mmss(until)}   fault removed (the red band ends); the 30 s burn rate falls within about 30 s",
        f"  +{mmss(total)}   run ends. The alert resolves a little later, once the 2-minute window has emptied (about 2 minutes after the fault stopped)",
    ])


def _cmd_demo(a) -> int:
    """A guided 4.5-minute incident for the dashboards and the screen recording. Uses the same fault as scenario s5."""
    from . import dashboards, runner
    s = dashboards.gather()
    good, nxt = dashboards.decide(s)
    if not good:
        print(dashboards.report(s))
        print("\nNEXT: " + nxt)
        return 1
    sc = cfg.load_scenario(DEMO_SCENARIO)
    ds = {d["uid"]: d for d in dashboards.catalog()}
    print("DEMO: one fault, from alert to recovery. Policy: " + a.policy + ". Nothing here is part of the experiment results (phase X).\n")
    print("1. Open these three tabs (time range: last 15 minutes, refresh 5 s):")
    for uid, d in ds.items():
        print(f"     {d['title']:36s} {dashboards.url(uid, s['grafana'])}")
    print("2. Start your screen recording (Windows: Win+Alt+R, or Win+G).\n3. Expect this timeline:\n" + demo_timeline(sc) + "\n")
    if not a.yes and sys.stdin.isatty():
        input("Press Enter to start the run (Ctrl+C to cancel)... ")
    runner.run_one("X", DEMO_SCENARIO, a.policy, 1, force=True)
    print("\nDone. Take your screenshots now (Service health: budget tile + burn rate; Incident view: alert timeline + stage latency). "
          "The alert is still resolving for about 2 more minutes: that is the recovery shot. "
          f"Adding your own marker while recording: slo-lab annotate 'what you did'. Results of this run: results/runs/X/{DEMO_SCENARIO}__{a.policy}__r1")
    return 0


def _cmd_list(_a) -> int:
    for s in cfg.list_scenarios():
        sc = cfg.load_scenario(s)
        print(f"{s:24s} {sc.get('description', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="slo-lab", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("latency", help="measure idle per-class latency (1-3 min)")
    s.add_argument("--per-class", type=int, default=4)
    s.set_defaults(fn=_cmd_latency)

    s = sub.add_parser("profile", help="list or switch the workload profile (request sizes)")
    s.add_argument("name", nargs="?", default=None)
    s.set_defaults(fn=_cmd_profile)

    s = sub.add_parser("capacity", help="probe single-replica capacity and set base_rps")
    s.add_argument("--step-s", type=float, default=45.0)
    s.set_defaults(fn=_cmd_capacity)

    s = sub.add_parser("set-base", help="recompute base_rps = FRAC x measured capacity without re-running the probe")
    s.add_argument("--frac", type=float, required=True)
    s.set_defaults(fn=_cmd_set_base)

    s = sub.add_parser("calibrate", help="derive SLO TTFT thresholds from a clean baseline")
    s.add_argument("--duration", type=float, default=None, help="seconds (default: automatic, 600-1200 s)")
    s.set_defaults(fn=_cmd_calibrate)

    s = sub.add_parser("run", help="run one experiment")
    s.add_argument("--phase", default="A", choices=["A", "B", "X"])
    s.add_argument("--scenario", required=True)
    s.add_argument("--policy", default="static1", choices=cfg.ALL_POLICIES)
    s.add_argument("--rep", type=int, default=1)
    s.add_argument("--force", action="store_true")
    s.add_argument("--duration", type=float, default=None, help="override scenario duration (smoke tests)")
    s.set_defaults(fn=_cmd_run)

    s = sub.add_parser("matrix", help="run a whole phase of the experiment matrix")
    s.add_argument("--phase", required=True, choices=["A", "B"])
    s.add_argument("--reps", type=int, default=3)
    s.add_argument("--only", nargs="*", help="restrict to these scenarios")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=_cmd_matrix)

    s = sub.add_parser("recover", help="repair the lab after a failed/interrupted run (heal + re-apply a failed Helm release)")
    s.set_defaults(fn=_cmd_recover)

    s = sub.add_parser("analyze", help="build results/REPORT.md from finished runs")
    s.add_argument("--include-invalid", action="store_true")
    s.set_defaults(fn=_cmd_analyze)

    s = sub.add_parser("robust", help="paired statistics + results with/without the accepted runs -> results/ROBUSTNESS.md")
    s.add_argument("--include-invalid", action="store_true")
    s.set_defaults(fn=_cmd_robust)

    s = sub.add_parser("summarize", help="print latency + detector results for one run directory")
    s.add_argument("run", help="e.g. results/runs/X/s0_baseline__static1__r1")
    s.add_argument("--pages", action="store_true", help="also list every page of every detector with a TRUE/FALSE verdict and what triggered it")
    s.add_argument("--grace", type=float, default=60.0, help="grace period after the incident end in which a page still counts as TRUE (default 60 s)")
    s.set_defaults(fn=_cmd_summarize)

    s = sub.add_parser("autoscaling", help="apply config/autoscaling.yaml (cpu/queue/burn thresholds) to the running lab and show the effective values")
    s.set_defaults(fn=_cmd_autoscaling)

    s = sub.add_parser("simulate", help="PREDICT how the cpu/queue/burn triggers behave at baseline load and in a burst (no cluster needed)")
    s.add_argument("--policies", nargs="*", default=["queue", "burn", "cpu"], choices=["queue", "burn", "cpu"])
    s.add_argument("--service-s", type=float, default=None, help="mean solo service time of one request in seconds (default 2.93, the idle service time measured by `make latency` on the reference machine)")
    s.add_argument("--rps", type=float, default=None, help="baseline requests/s (default: base_rps from config/load.yaml)")
    s.add_argument("--threshold", type=float, default=None, help="override the trigger threshold (default: the chart defaults / config/autoscaling.yaml)")
    s.add_argument("--burn-window-s", type=float, default=30.0)
    s.add_argument("--reps", type=int, default=60)
    s.set_defaults(fn=_cmd_simulate)

    s = sub.add_parser("share", help="pack results/ + config/ into ONE zip you can attach to a chat (copies it to Windows Downloads when unambiguous)")
    s.add_argument("--out", default=None, help="zip path (default: results-share-<timestamp>.zip in the project folder)")
    s.add_argument("--max-file-mb", type=float, default=25.0, help="leave out single files larger than this (default 25)")
    s.add_argument("--no-copy", action="store_true", help="do not copy the zip to the Windows Downloads folder")
    s.set_defaults(fn=_cmd_share)

    s = sub.add_parser("health", help="per-run host-health table (steady-stretch good%%/TTFT, host CPU and steal if `make hostlog` ran): which runs were measured on a slow host?")
    s.add_argument("--phase", default=None, choices=["A", "B", "X", "G"], help="one phase (default: all)")
    s.add_argument("--rerun", action="store_true", help="re-run every run flagged BAD, SLOW or INVALID (refused unless a gate passed in the last 90 min)")
    s.add_argument("--accept", action="store_true", help="keep the runs that are STILL flagged BAD/SLOW after a re-run (reproducible, not host noise); refused unless a gate passed in the last 90 min; INVALID runs are never accepted")
    s.add_argument("--no-gate-check", action="store_true", help="with --rerun or --accept: skip the recent-gate check (not recommended)")
    s.set_defaults(fn=_cmd_health)

    s = sub.add_parser("gate", help="HEALTH GATE (~12 min): one idle baseline run; exit code 1 when fewer than --min-good %% of requests are good")
    s.add_argument("--min-good", type=float, default=97.0, help="minimum good %% to pass (default 97)")
    s.add_argument("--history", action="store_true", help="only print every earlier gate result (no run)")
    s.set_defaults(fn=_cmd_gate)

    s = sub.add_parser("dashboards", help="where are the three Grafana dashboards, are they loaded, does Prometheus have what they draw? Read-only, about 2 s")
    s.add_argument("--open", action="store_true", help="also open the Service health dashboard in your browser")
    s.set_defaults(fn=_cmd_dashboards)

    s = sub.add_parser("annotate", help="add a marker (for example: deployed v2) to every Grafana time series")
    s.add_argument("text")
    s.add_argument("--tag", action="append", default=[], help="extra tag; repeatable")
    s.set_defaults(fn=_cmd_annotate)

    s = sub.add_parser("demo", help="guided 4.5-minute incident for the dashboards and a screen recording (one retriever fault, alert to recovery; phase X, not part of the results)")
    s.add_argument("--policy", default="cpu", choices=cfg.ALL_POLICIES)
    s.add_argument("--yes", action="store_true", help="do not wait for Enter")
    s.set_defaults(fn=_cmd_demo)

    s = sub.add_parser("status", help="where does the lab stand (Docker, cluster, host logger, finished runs, last gate) and what is the ONE next command? Read-only, about 10 s")
    s.set_defaults(fn=_cmd_status)

    s = sub.add_parser("scenarios", help="list scenarios")
    s.set_defaults(fn=_cmd_list)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (k8s.LabNotReady, KeyError) as e:
        print(f"\n[not ready] {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"\n[error] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[abort] interrupted; cleaning up faults", file=sys.stderr)
        try:
            from . import runner
            runner.cleanup_faults()
        except Exception:  # noqa: BLE001
            pass
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
