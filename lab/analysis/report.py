"""Aggregate analysis tables into CSVs, figures and a markdown report."""
from __future__ import annotations

import datetime as dt

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .. import config as cfg  # noqa: E402
from .pipeline import analyze_phase  # noqa: E402


def bootstrap_ci(values, stat=np.median, n: int = 2000, seed: int = 0, alpha: float = 0.05):
    v = np.asarray([x for x in values if x is not None and not np.isnan(x)], dtype=float)
    if len(v) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    stats = [stat(rng.choice(v, size=len(v), replace=True)) for _ in range(n)]
    return (float(np.quantile(stats, alpha / 2)), float(np.quantile(stats, 1 - alpha / 2)))


def md_table(df: pd.DataFrame, floatfmt: str = "{:.2f}") -> str:
    if df.empty:
        return "_no data_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if isinstance(v, (float, np.floating)):
                cells.append("n/a" if np.isnan(v) else floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def aggregate_detection(det: pd.DataFrame) -> pd.DataFrame:
    if det.empty:
        return pd.DataFrame()
    rows = []
    for (scenario, detector), g in det.groupby(["scenario", "detector"]):
        inc = g[g["has_incident"]]
        ttds = inc["ttd_s"].dropna().tolist()
        lo, hi = bootstrap_ci(ttds)
        fph = g["false_pages_per_hour"].dropna()
        rows.append({
            "scenario": scenario, "detector": detector, "runs": len(g),
            "detect_rate": float(inc["detected"].mean()) if len(inc) else float("nan"),
            "ttd_median_s": float(np.median(ttds)) if ttds else float("nan"),
            "ttd_ci_lo": lo, "ttd_ci_hi": hi,
            "false_pages_per_hr": float(fph.mean()) if len(fph) else float("nan"),
        })
    return pd.DataFrame(rows).sort_values(["scenario", "detector"]).reset_index(drop=True)


def aggregate_autoscale(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    rows = []
    for (scenario, policy), g in summary.groupby(["scenario", "policy"]):
        b = g["budget_consumed_request"].tolist()
        lo, hi = bootstrap_ci(b, stat=np.mean)
        rows.append({
            "scenario": scenario, "policy": policy, "runs": len(g),
            "budget_mean": float(np.mean(b)), "budget_ci_lo": lo, "budget_ci_hi": hi,
            "budget_token_mean": float(g["budget_consumed_token"].mean()),
            "p95_ttft_all_s": float(g["ttft_p95_all"].mean()),
            "error_ratio": float(g["error_ratio"].mean()),
            "mean_replicas": float(g["mean_ready_replicas"].mean()) if g["mean_ready_replicas"].notna().any() else float("nan"),
            "time_at_1_replica": float(g["frac_time_1_replica"].mean()) if "frac_time_1_replica" in g and g["frac_time_1_replica"].notna().any() else float("nan"),
        })
    return pd.DataFrame(rows).sort_values(["scenario", "policy"]).reset_index(drop=True)


def aggregate_rca(r: pd.DataFrame) -> pd.DataFrame:
    if r.empty:
        return pd.DataFrame()
    rows = []
    for scenario, g in r.groupby("scenario"):
        rows.append({
            "scenario": scenario, "truth": g["truth"].iloc[0], "incidents": len(g),
            "attribution_acc": float(g["attribution_correct"].mean()),
            "abstain_rate": float(g["attribution_abstained"].mean()),
            "always_llm_acc": float(g["always_llm_correct"].mean()),
            "random_expected": float(g["random_expected"].mean()),
            # two more estimators computed from the same runs (see lab/analysis/rca.py); NOT pre-registered, reported next to the naive one
            "class_adjusted_acc": float(g["adjusted_correct"].mean()) if "adjusted_correct" in g else float("nan"),
            "failure_tagged_acc": float(g["tagged_correct"].mean()) if "tagged_correct" in g else float("nan"),
        })
    return pd.DataFrame(rows)


GRACES_S = (60.0, 120.0, 180.0)


def grace_sensitivity(phase: str = "A", include_invalid: bool = False) -> pd.DataFrame:
    """How much do detection rate and false pages depend on the grace period that decides TRUE vs FALSE pages?

    Pages are TRUE only if they start inside [incident start, incident end + grace]. A detector whose long window still
    remembers a finished incident can re-fire just after the grace period; this table shows how many of the 'false pages'
    are of that kind (they disappear as the grace grows) and how many are genuinely unrelated to the incident.
    """
    rows = []
    for g in GRACES_S:
        det = analyze_phase(phase, include_invalid, grace_s=g)["detection"]
        if det.empty:
            continue
        for name, d in det.groupby("detector"):
            inc = d[d["has_incident"]]
            fph = d["false_pages_per_hour"].dropna()
            rows.append({"detector": name, "grace_s": g,
                         "detect_rate": float(inc["detected"].mean()) if len(inc) else float("nan"),
                         "false_pages_total": int(d["false_pages"].sum()),
                         "false_pages_per_hr": float(fph.mean()) if len(fph) else float("nan")})
    return pd.DataFrame(rows)


def _fig_ttd(agg: pd.DataFrame, out) -> None:
    d = agg.dropna(subset=["ttd_median_s"])
    if d.empty:
        return
    piv = d.pivot(index="scenario", columns="detector", values="ttd_median_s")
    ax = piv.plot(kind="bar", figsize=(11, 5))
    ax.set_ylabel("median time-to-detect (s)")
    ax.set_title("Phase A: time-to-detect by scenario and detector (missed incidents omitted)")
    plt.tight_layout()
    plt.savefig(out, dpi=130)
    plt.close()


def _fig_budget(agg: pd.DataFrame, out) -> None:
    if agg.empty:
        return
    piv = agg.pivot(index="scenario", columns="policy", values="budget_mean")
    ax = piv.plot(kind="bar", figsize=(10, 5))
    ax.axhline(1.0, color="k", ls="--", lw=1)
    ax.set_ylabel("error budget consumed over the run (1.0 = SLO exactly met)")
    ax.set_title("Phase B: budget consumed by autoscaling policy")
    plt.tight_layout()
    plt.savefig(out, dpi=130)
    plt.close()


def build_report(include_invalid: bool = False) -> str:
    cfg.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    parts = [f"# SLO-Loop Lab results\n\n_Generated {dt.datetime.now().isoformat(timespec='seconds')}_\n"]
    slo_cfg = cfg.load_slo()
    parts.append(f"SLO target: {slo_cfg['target']}; TTFT thresholds (s): {slo_cfg['ttft_s']}; calibrated={slo_cfg.get('calibrated')}\n")

    a = analyze_phase("A", include_invalid)
    parts.append("\n## Phase A - detection (H1/H2)\n")
    if a["detection"].empty:
        parts.append("_no valid Phase A runs yet_\n")
    else:
        agg = aggregate_detection(a["detection"])
        agg.to_csv(cfg.RESULTS_DIR / "phaseA_detection.csv", index=False)
        parts.append(md_table(agg))
        if "time_scale" in a["detection"].columns:
            sc = a["detection"].groupby("detector")["time_scale"].median()
            parts.append("\nEach detector's windows were stretched to match the request rate (median factor, x1 = as defined): "
                         + ", ".join(f"{k} x{v:g}" for k, v in sc.items())
                         + ". Rules whose stretched long window did not fit in the run were not evaluated (`rules_dropped`).\n")
        _fig_ttd(agg, cfg.RESULTS_DIR / "fig_phaseA_ttd.png")
        parts.append("\n![ttd](fig_phaseA_ttd.png)\n")
        rc = aggregate_rca(a["rca"])
        rc.to_csv(cfg.RESULTS_DIR / "phaseA_rca.csv", index=False)
        parts.append("\n## Phase A - root-cause attribution baseline (H4)\n")
        parts.append(md_table(rc))
        parts.append("\n`attribution_acc` is the original latency-only estimator (pooled stage medians). `class_adjusted_acc` compares each request with "
                     "the baseline of its own prompt class; `failure_tagged_acc` additionally uses which hop a failed request names (http_503 = retriever, "
                     "llm_error/timeout = llm). The last two were designed AFTER the Phase A data had been seen: they are exploratory, and a claim needs fresh "
                     "repetitions that were not used to design them.\n")
        gs = grace_sensitivity("A", include_invalid)
        if not gs.empty:
            gs.to_csv(cfg.RESULTS_DIR / "phaseA_grace_sensitivity.csv", index=False)
            parts.append("\n## Phase A - sensitivity to the grace period\n")
            parts.append(md_table(gs))
            parts.append("\nA page is TRUE only if it starts inside [incident start, incident end + grace]. If false pages vanish as the grace grows they are "
                         "late echoes of the incident (long windows still remembering it); if they stay, they are unrelated to it. "
                         "`summarize --pages` shows each one.\n")

    b = analyze_phase("B", include_invalid)
    parts.append("\n## Phase B - autoscaling (H3)\n")
    if b["summary"].empty:
        parts.append("_no valid Phase B runs yet_\n")
    else:
        agg = aggregate_autoscale(b["summary"])
        agg.to_csv(cfg.RESULTS_DIR / "phaseB_autoscale.csv", index=False)
        parts.append(md_table(agg))
        _fig_budget(agg, cfg.RESULTS_DIR / "fig_phaseB_budget.png")
        parts.append("\n![budget](fig_phaseB_budget.png)\n")

    parts.append(
        "\n## Read this before quoting any number\n"
        "- Single laptop, shared CPU: absolute latencies are not portable; compare policies within this report only.\n"
        "- Small repetition counts (see `runs`): the bootstrap intervals are wide for a reason. Use >=5 reps for headline claims.\n"
        "- Windows are time-compressed (x60) relative to production burn-rate alerting, then stretched to the run's request rate; see docs/DESIGN.md.\n"
        "- The RCA baseline is stage attribution from gateway timings, not distributed tracing.\n"
    )
    text = "\n".join(parts)
    (cfg.RESULTS_DIR / "REPORT.md").write_text(text, encoding="utf-8")
    return text
