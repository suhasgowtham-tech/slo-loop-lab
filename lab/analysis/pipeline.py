"""Turn run directories into analysis tables (offline; no cluster needed)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config as cfg
from . import rca, slo
from .detect import Detector, alert_intervals, auto_time_scale, detector_from_dict, evaluate, scale_detector, score


def _replica_stats(run: Path, start_epoch: float, end_epoch: float) -> dict:
    p = run / "replicas.csv"
    if not p.exists():
        return {"mean_ready_replicas": None, "replica_seconds": None}
    r = pd.read_csv(p)
    r = r[(r["epoch"] >= start_epoch - 5) & (r["epoch"] <= end_epoch + 5)]
    if len(r) < 2:
        return {"mean_ready_replicas": None, "replica_seconds": None}
    t = r["epoch"].to_numpy(dtype=float)
    ready = r["ready"].to_numpy(dtype=float)
    secs = float(np.trapezoid(ready, t)) if hasattr(np, "trapezoid") else float(np.trapz(ready, t))
    span = max(t[-1] - t[0], 1e-9)
    dt = np.diff(t)  # step function: sample i holds until sample i+1
    at_one = float(dt[ready[:-1] == 1].sum())
    spec = r["spec"].to_numpy(dtype=float)
    out = [float(x - start_epoch) for x, s_ in zip(t, spec) if s_ >= 2]
    return {"mean_ready_replicas": secs / span, "replica_seconds": secs,
            "frac_time_1_replica": at_one / span, "max_ready_replicas": float(ready.max()),
            "first_scale_out_s": (out[0] if out else None)}


def replica_timeline(run: Path, start_epoch: float) -> list[tuple[float, int, int]]:
    """Change points of the llm tier as (seconds since the run started, desired replicas, ready replicas).

    Answers "did the autoscaler scale up, how long did the new pod take to become ready, and did it scale back down?"
    """
    p = run / "replicas.csv"
    if not p.exists():
        return []
    r = pd.read_csv(p)
    out: list[tuple[float, int, int]] = []
    prev = None
    for epoch, spec, ready in zip(r["epoch"], r["spec"], r["ready"]):
        cur = (int(spec), int(ready))
        if cur != prev:
            out.append((float(epoch) - start_epoch, cur[0], cur[1]))
            prev = cur
    return out


def _run_detector(df: pd.DataFrame, det: Detector, target: float, duration: float, time_scale: float | str):
    """(stretch factor, scaled detector, dropped rules, page intervals) for one detector on one run."""
    factor = auto_time_scale(len(df), duration, det) if time_scale == "auto" else float(time_scale)
    used, dropped = scale_detector(det, factor, 0.8 * duration if factor != 1.0 else None)
    intervals = []
    if used.rules:  # no rule fits in this run: "not evaluable", never a fake zero-alert result
        grid, fire = evaluate(df, used, target, duration)
        intervals = alert_intervals(grid, fire)
    return factor, used, dropped, intervals


def status_counts(df: pd.DataFrame, lo: float | None = None, hi: float | None = None) -> dict[str, int]:
    """Count requests by status, optionally only those SENT in [lo, hi)."""
    d = df if lo is None else df[(df["t_send"] >= lo) & (df["t_send"] < hi)]
    return {str(k): int(v) for k, v in d["status"].value_counts().items()}


def page_report(run: Path, detectors: list[Detector], grace_s: float = 60.0, time_scale: float | str = "auto") -> list[dict]:
    """Every page of every detector, labelled against the ground-truth incident window.

    verdict: TRUE if the page STARTS inside [incident.start, incident.end + grace] (exactly the rule in detect.score),
    otherwise FALSE with where it fell (before the incident / after the grace period). For a FALSE page, `bad_before`
    lists the bad requests that completed in the 120 s before the page started: the events that made it fire.
    """
    meta = json.loads((run / "meta.json").read_text())
    events = json.loads((run / "events.json").read_text())
    s = meta["slo"]
    df = slo.annotate(slo.load_requests(run / "requests.csv"), s["ttft_s"])
    duration = float(meta["duration_s"])
    incidents = events["incidents"]
    rows = []
    for det in detectors:
        factor, _, _, intervals = _run_detector(df, det, s["target"], duration, time_scale)
        for a, b in intervals:
            where, verdict = "no incident in this scenario", "FALSE"
            for inc in incidents:
                lo, hi = float(inc["start"]), float(inc["end"])
                if lo <= a <= hi:
                    where, verdict = "inside the incident", "TRUE"
                    break
                if hi < a <= hi + grace_s:
                    where, verdict = f"{a - hi:.0f} s after the incident ended (inside the grace period)", "TRUE"
                    break
                where = (f"{lo - a:.0f} s before the incident started" if a < lo
                         else f"{a - hi:.0f} s after the incident ended (outside the {grace_s:.0f} s grace period)")
            near = df[(~df["good"]) & (df["t_done"] > a - 120.0) & (df["t_done"] <= a)]
            bad = [(float(r.t_done), str(r.cls), str(r.status)) for r in near.itertuples()][-5:] if verdict == "FALSE" else []
            rows.append({"detector": det.name, "start": float(a), "end": float(b), "verdict": verdict, "where": where,
                         "bad_before": bad, "x": factor})
    return rows


def analyze_run(run: Path, detectors: list[Detector], grace_s: float = 60.0, time_scale: float | str = 1.0) -> dict:
    """`time_scale`: 1.0 = detectors exactly as defined; a number stretches every window by that factor; "auto" picks
    the smallest stretch that gives the shortest short window enough expected events at this run's request rate."""
    meta = json.loads((run / "meta.json").read_text())
    events = json.loads((run / "events.json").read_text())
    s = meta["slo"]
    df = slo.annotate(slo.load_requests(run / "requests.csv"), s["ttft_s"])
    duration = float(meta["duration_s"])
    incidents = events["incidents"]
    base = {"run": run.name, "phase": meta["phase"], "scenario": meta["scenario"], "policy": meta["policy"],
            "rep": meta["rep"], "valid": meta["valid"]}

    detection = []
    factors = []
    for det in detectors:
        factor, used, dropped, intervals = _run_detector(df, det, s["target"], duration, time_scale)
        factors.append(factor)
        sc = score(intervals, incidents, duration, grace_s)
        detection.append({**base, "detector": det.name, "sli": det.sli, **sc, "time_scale": factor,
                          "rules_evaluated": len(used.rules), "rules_dropped": len(dropped)})

    summary = {
        **base,
        "n_requests": int(len(df)),
        "budget_consumed_request": slo.budget_consumed(df, s["target"], 0.0, duration, "request"),
        "budget_consumed_token": slo.budget_consumed(df, s["target"], 0.0, duration, "token"),
        "time_scale": max(factors) if factors else 1.0,
        "good_ratio": float(df["good"].mean()) if len(df) else None,
        "error_ratio": float((df["status"] != "ok").mean()) if len(df) else None,
        **_replica_stats(run, meta["start_epoch"], meta["end_epoch"]),
    }
    st = slo.class_stats(df)
    for c in (*slo.CLASSES, "all"):
        summary[f"ttft_p95_{c}"] = st[c]["ttft_p95"]

    rca_rows = []
    for inc in incidents:
        if inc["start"] >= 60 and "root_cause" in inc:
            base_w, inc_w = (20.0, inc["start"] - 10.0), (float(inc["start"]), float(inc["end"]))
            res = rca.localize(df, base_w, inc_w)
            adj = rca.localize_adjusted(df, base_w, inc_w)
            tag = rca.localize_tagged(df, base_w, inc_w)
            in_window = (df["t_send"] >= float(inc["start"])) & (df["t_send"] < float(inc["end"]))
            rca_rows.append({**base, "truth": inc["root_cause"], "pred": res["pred"], "abstain": res["abstain"],
                             "n_inc": res["n_inc"], "n_base": res["n_base"], "scores": res["scores"],
                             "n_inc_failed": int((in_window & (df["status"] != "ok")).sum()),
                             "pred_adjusted": adj["pred"], "scores_adjusted": adj["scores"],
                             "pred_tagged": tag["pred"], "scores_tagged": tag["scores"], "evidence_tagged": tag.get("evidence"),
                             "adjusted_correct": adj["pred"] == inc["root_cause"], "tagged_correct": tag["pred"] == inc["root_cause"],
                             **rca.score_methods(res["pred"], inc["root_cause"])})
    return {"detection": detection, "summary": summary, "rca": rca_rows}


def analyze_phase(phase: str, include_invalid: bool = False, grace_s: float = 60.0,
                  time_scale: float | str = "auto") -> dict[str, pd.DataFrame]:
    detectors = [detector_from_dict(d) for d in cfg.load_detectors()]
    runs = sorted(p for p in (cfg.RUNS_DIR / phase).glob("*") if (p / "DONE").exists())
    det_rows, sum_rows, rca_rows = [], [], []
    for r in runs:
        meta = json.loads((r / "meta.json").read_text())
        if not meta["valid"] and not include_invalid:
            continue
        a = analyze_run(r, detectors, grace_s, time_scale)
        det_rows += a["detection"]
        sum_rows.append(a["summary"])
        rca_rows += a["rca"]
    return {"detection": pd.DataFrame(det_rows), "summary": pd.DataFrame(sum_rows), "rca": pd.DataFrame(rca_rows)}
