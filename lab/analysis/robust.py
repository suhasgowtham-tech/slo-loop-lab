"""Robustness checks and paired statistics for the finished Phase A / Phase B data (offline, no cluster).

Why this exists. `make analyze` gives one table per phase. Three things in it need more care before a claim is made:

* Every detector is evaluated on the SAME runs, and every autoscaling policy is evaluated on the SAME traffic (the seed depends on
  scenario + rep only). The honest comparison is therefore PAIRED: "on how many runs did A find it and B did not (and the reverse)?"
  and "in how many (scenario, rep) pairs did policy A burn less budget than policy B?". An exact two-sided sign test says how often a
  split that lopsided happens by chance. No multiple-comparison correction is applied: read p between 0.01 and 0.05 as weak.
* Pooled detect rates over-weight s7_slow_burn (13 reps against 6 for every other scenario), so each rate is also shown per scenario
  group (macro average, s7 alone, everything except s7).
* Runs that a person kept after a re-run (results/accepted-flags.csv) and runs measured without the host logger might drive a result.
  Every table is shown with and without them, so a conclusion that only holds with them is visible.

Nothing here decides a hypothesis by itself: it prints counts, intervals and p-values next to each other.
"""
from __future__ import annotations

import math

import pandas as pd

from .. import config as cfg
from .. import gate
from .pipeline import analyze_phase
from .report import md_table

S7 = "s7_slow_burn"
# H1: does the token / class SLI find a prompt-mix problem sooner or more often than the request SLI (same thresholds)?
SLI_PAIRS = [("tuned_class", "tuned_request"), ("tuned_token", "tuned_request"),
             ("google_x60_class", "google_x60_request"), ("google_x60_token", "google_x60_request")]
# H2: do the workbook thresholds (time-compressed x60) lose to thresholds tuned for this system (same SLI)?
THRESHOLD_PAIRS = [("tuned_request", "google_x60_request"), ("tuned_class", "google_x60_class"), ("tuned_token", "google_x60_token")]
# H3: pairs of autoscaling policies, same traffic.
POLICY_PAIRS = [("cpu", "queue"), ("cpu", "burn"), ("cpu", "static1"), ("cpu", "static3"), ("queue", "static1"), ("burn", "static1"), ("burn", "queue")]


# ------------------------------------------------------------------------------------------------------------------ statistics
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion (better than the normal interval for small n and for rates near 0 or 1)."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def sign_test_p(a_only: int, b_only: int) -> float:
    """Exact two-sided sign test on the discordant pairs (H0: each is equally likely to be the one that wins). 1.0 if there are none."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = min(a_only, b_only)
    return min(1.0, 2.0 * sum(math.comb(n, i) for i in range(k + 1)) / 2.0 ** n)


# ------------------------------------------------------------------------------------------------------------------ Phase A: detection
def detector_summary(det: pd.DataFrame) -> pd.DataFrame:
    """One row per detector: found/incident runs pooled, Wilson interval, macro average over scenarios, s7 alone, all but s7, false pages."""
    if det.empty:
        return pd.DataFrame()
    inc = det[det["has_incident"]]
    quiet_total = float(det.drop_duplicates("run")["quiet_hours"].sum()) if "quiet_hours" in det else float("nan")
    rows = []
    for name, g in inc.groupby("detector"):
        k, n = int(g["detected"].sum()), len(g)
        lo, hi = wilson(k, n)
        s7, rest = g[g["scenario"] == S7], g[g["scenario"] != S7]
        fp = int(det[det["detector"] == name]["false_pages"].sum())
        rows.append({
            "detector": name, "found": f"{k}/{n}", "rate": k / n, "ci_lo": lo, "ci_hi": hi,
            "macro_rate": float(g.groupby("scenario")["detected"].mean().mean()),
            S7: f"{int(s7['detected'].sum())}/{len(s7)}" if len(s7) else "-",
            "all_but_s7": f"{int(rest['detected'].sum())}/{len(rest)}" if len(rest) else "-",
            "false_pages": fp,
            "false_per_quiet_hr": fp / quiet_total if quiet_total and quiet_total > 0 else float("nan"),
        })
    return pd.DataFrame(rows).sort_values("detector").reset_index(drop=True)


def paired_detection(det: pd.DataFrame, a: str, b: str, scenarios: list[str] | None = None) -> dict | None:
    """A against B on the same incident runs: who found it alone, and how much sooner (median over runs where both found it)."""
    d = det[det["has_incident"] & det["detector"].isin([a, b])]
    if scenarios is not None:
        d = d[d["scenario"].isin(scenarios)]
    if d.empty:
        return None
    found = d.pivot(index="run", columns="detector", values="detected")
    if a not in found or b not in found:
        return None
    found = found.dropna().astype(bool)
    if found.empty:
        return None
    ttd = d.pivot(index="run", columns="detector", values="ttd_s").reindex(found.index)
    both = found[a] & found[b]
    diff = (ttd.loc[both, a].astype(float) - ttd.loc[both, b].astype(float)).dropna()
    a_only, b_only = int((found[a] & ~found[b]).sum()), int((~found[a] & found[b]).sum())
    return {"a": a, "b": b, "runs": len(found), "a_found": int(found[a].sum()), "b_found": int(found[b].sum()),
            "a_only": a_only, "b_only": b_only, "p_sign": sign_test_p(a_only, b_only), "both": int(both.sum()),
            "ttd_a_minus_b_median_s": float(diff.median()) if len(diff) else float("nan")}


def paired_detection_rows(det: pd.DataFrame, pairs: list[tuple[str, str]], per_scenario: bool = False) -> pd.DataFrame:
    if det.empty:
        return pd.DataFrame()
    scen = sorted(det[det["has_incident"]]["scenario"].unique())
    scopes: list[tuple[str, list[str] | None]] = [("ALL", None)]
    if S7 in scen:
        scopes += [(S7, [S7]), ("all but s7", [s for s in scen if s != S7])]
    if per_scenario:
        scopes += [(s, [s]) for s in scen if s != S7]
    rows = []
    for a, b in pairs:
        for label, sc in scopes:
            r = paired_detection(det, a, b, sc)
            if r is not None:
                rows.append({"pair": f"{a} vs {b}", "scope": label, **{k: v for k, v in r.items() if k not in ("a", "b")}})
    return pd.DataFrame(rows)


def sensitivity_detection(det_by_variant: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """detector x variant: found/incident runs, so a result that only holds with the kept or unlogged runs shows up."""
    out: dict[str, dict] = {}
    for v, det in det_by_variant.items():
        if det.empty:
            continue
        for name, g in det[det["has_incident"]].groupby("detector"):
            k, n = int(g["detected"].sum()), len(g)
            out.setdefault(name, {"detector": name})[v] = f"{k}/{n} ({100 * k / n:.0f}%)" if n else "-"
    return pd.DataFrame(list(out.values())).sort_values("detector").reset_index(drop=True) if out else pd.DataFrame()


# ------------------------------------------------------------------------------------------------------------------ Phase A: attribution
def rca_summary(r: pd.DataFrame) -> pd.DataFrame:
    """Attribution accuracy per scenario and pooled over the retriever incidents, with Wilson intervals; abstentions are counted as misses
    (as in the main table) and also shown separately."""
    if r.empty:
        return pd.DataFrame()

    def row(label: str, g: pd.DataFrame) -> dict:
        n = len(g)
        k = int(g["attribution_correct"].sum())
        answered = int((~g["attribution_abstained"].astype(bool)).sum())
        lo, hi = wilson(k, n)
        return {"scenario": label, "truth": str(g["truth"].iloc[0]), "incidents": n, "correct": f"{k}/{n}", "acc": k / n, "ci_lo": lo, "ci_hi": hi,
                "answered": answered, "acc_if_answered": (k / answered) if answered else float("nan"),
                "always_llm": float(g["always_llm_correct"].mean()), "chance": float(g["random_expected"].mean()),
                "exploratory_tagged": f"{int(g['tagged_correct'].sum())}/{n}" if "tagged_correct" in g else "-"}

    rows = [row(s, g) for s, g in r.groupby("scenario")]
    ret = r[r["truth"] == "retriever"]
    if len(ret) and ret["scenario"].nunique() > 1:
        rows.append(row("RETRIEVER pooled", ret))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------------------------ Phase B: autoscaling
def budget_by_variant(summaries: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """scenario x policy: mean budget consumed (x the budget; 1.0 = SLO exactly met) per variant, plus mean ready replicas."""
    out = None
    for v, s in summaries.items():
        if s.empty:
            continue
        g = s.groupby(["scenario", "policy"]).agg(n=("budget_consumed_request", "size"), budget=("budget_consumed_request", "mean"),
                                                   replicas=("mean_ready_replicas", "mean")).reset_index()
        g = g.rename(columns={c: (c if v == "all" else f"{c}_{v}") for c in ("n", "budget", "replicas")})
        if v != "all":
            g = g.drop(columns=[c for c in g.columns if c.startswith("replicas_")])
        out = g if out is None else out.merge(g, on=["scenario", "policy"], how="left")
    return out if out is not None else pd.DataFrame()


def paired_budget(summary: pd.DataFrame, a: str, b: str, scenarios: list[str] | None = None) -> dict | None:
    """Policy A against policy B on the same (scenario, rep) = the same traffic. Ties (e.g. 0 against 0) carry no information and are left out."""
    if summary.empty:
        return None
    s = summary if scenarios is None else summary[summary["scenario"].isin(scenarios)]
    x = s.set_index(["scenario", "rep", "policy"])
    bud = x["budget_consumed_request"].unstack("policy")
    rep = x["mean_ready_replicas"].unstack("policy")
    if a not in bud or b not in bud:
        return None
    ok = bud[a].notna() & bud[b].notna()
    if not ok.any():
        return None
    d = (bud.loc[ok, a] - bud.loc[ok, b]).astype(float)
    lower, higher = int((d < -1e-9).sum()), int((d > 1e-9).sum())
    rd = (rep.loc[ok, a] - rep.loc[ok, b]).astype(float).dropna() if a in rep and b in rep else pd.Series(dtype=float)
    return {"pairs": int(ok.sum()), "a_burns_less": lower, "a_burns_more": higher, "ties": int(ok.sum()) - lower - higher,
            "p_sign": sign_test_p(lower, higher), "mean_budget_diff": float(d.mean()),
            "mean_replica_diff": float(rd.mean()) if len(rd) else float("nan")}


def paired_budget_rows(summary: pd.DataFrame, pairs: list[tuple[str, str]], per_scenario: bool = False) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    scen = sorted(summary["scenario"].unique())
    scopes: list[tuple[str, list[str] | None]] = [("ALL", None)] + ([(s, [s]) for s in scen] if per_scenario else [])
    rows = []
    for a, b in pairs:
        for label, sc in scopes:
            r = paired_budget(summary, a, b, sc)
            if r is not None:
                rows.append({"pair": f"{a} vs {b}", "scope": label, **r})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------------------------ pre-registered confirmation
# docs/PREREGISTRATION-confirmation.md: the reps that did NOT exist when the hypotheses were read off the first data (18 Nov 2026).
FRESH = {("A", "s5_retriever_latency"): 7, ("A", S7): 14, ("B", "s3_cpu_stress"): 4, ("B", "s8_ramp"): 4}
H4_EXPECTED = 18                    # 6 fresh s5 + 12 fresh s7 incidents
H3_SCENARIOS = ["s3_cpu_stress", "s8_ramp"]
H3_RULE_REPS = 5                    # the registered rule counts reps 1-5 of s3 and s8 = 10 pairs; reps beyond 5 (if run) count only in the fresh-reps scope
H3_RULE_PAIRS = 10


def _rep_of(run) -> int:
    try:
        return int(str(run).rsplit("__r", 1)[1])
    except (IndexError, ValueError):
        return -1


def fresh_only(df: pd.DataFrame, phase: str) -> pd.DataFrame:
    """Rows of an analysis table that belong to a rep that was added after the first reading of the data."""
    if df.empty or "run" not in df or "scenario" not in df:
        return df.iloc[0:0]
    reps = df["run"].map(_rep_of)
    floor = df["scenario"].map(lambda s: FRESH.get((phase, s), 10 ** 9))
    return df[reps >= floor].reset_index(drop=True)


def confirm_h4(rca: pd.DataFrame, accepted: set[tuple[str, str]]) -> tuple[pd.DataFrame, list[str]]:
    """Registered rule: on the fresh s5 + s7 incidents together, `failure_tagged` has a Wilson 95% lower bound above chance (1/3) AND beats always-llm."""
    fresh = fresh_only(rca, "A")
    if fresh.empty:
        return pd.DataFrame(), ["H4: no fresh Phase A runs yet (s5 r7+, s7 r14+)."]

    def row(label: str, g: pd.DataFrame) -> dict:
        n = len(g)
        k = int(g["tagged_correct"].sum())
        lo, hi = wilson(k, n)
        adj = f"{int(g['adjusted_correct'].sum())}/{n}" if "adjusted_correct" in g else "-"
        return {"group": label, "incidents": n, "naive": f"{int(g['attribution_correct'].sum())}/{n}", "class_adj": adj, "failure_tagged": f"{k}/{n}",
                "tag_ci_lo": lo, "tag_ci_hi": hi, "always_llm": f"{int(g['always_llm_correct'].sum())}/{n}", "chance": float(g["random_expected"].mean())}

    groups = [(s, g) for s, g in fresh.groupby("scenario")] + [("fresh pooled", fresh), ("fresh pooled, without accepted", _without(fresh, accepted))]
    table = pd.DataFrame([row(label, g) for label, g in groups if len(g)])
    lines = []
    for label, g in groups[-2:]:
        if g.empty:
            continue
        n, k = len(g), int(g["tagged_correct"].sum())
        lo = wilson(k, n)[0]
        chance, llm = float(g["random_expected"].mean()), float(g["always_llm_correct"].mean())
        ok = lo > chance and k / n > llm
        state = ("CONFIRMED" if ok else "NOT CONFIRMED") + ("" if n >= H4_EXPECTED else f" so far (INCOMPLETE: {n} of {H4_EXPECTED} fresh incidents)")
        lines.append(f"H4 [{label}]: failure_tagged {k}/{n}, Wilson lower bound {lo:.2f} vs chance {chance:.2f} and always-llm {llm:.2f} -> {state}")
    return table, lines


def confirm_h3(summary: pd.DataFrame, accepted: set[tuple[str, str]]) -> tuple[pd.DataFrame, list[str]]:
    """Registered rule: over the (scenario, rep) pairs of s3 and s8 (reps 1-5), the CPU policy burns less budget than `queue` AND than `burn` in at least
    80% of the non-tied pairs with an exact two-sided sign test p < 0.05 for each. The fresh reps alone (>= 4) are shown too. With reps 4-5 that is 4 pairs,
    and 4 pairs can never reach p < 0.05 (the smallest possible p is 0.125): they can only replicate the direction. With reps 4-6 it is 6 pairs (0.031)."""
    base = summary[summary["scenario"].isin(H3_SCENARIOS)] if not summary.empty else summary
    if base.empty:
        return pd.DataFrame(), ["H3: no s3 / s8 runs."]
    pooled = base[base["rep"] <= H3_RULE_REPS]
    floor = FRESH[("B", H3_SCENARIOS[0])]
    fresh_expected = len(H3_SCENARIOS) * (int(max(H3_RULE_REPS, base["rep"].max())) - floor + 1)
    fresh_label = f"fresh reps {floor}+ only"
    scopes = [("registered rule: reps 1-5 (includes the 3 reps that suggested it)", pooled, H3_RULE_PAIRS),
              ("registered rule, without accepted", _without(pooled, accepted), H3_RULE_PAIRS), (fresh_label, fresh_only(base, "B"), fresh_expected)]
    rows, verdicts = [], {}
    for label, s, expected in scopes:
        for other in ("queue", "burn"):
            r = paired_budget(s, "cpu", other) if len(s) else None
            if r is None:
                continue
            nonties = r["a_burns_less"] + r["a_burns_more"]
            share = r["a_burns_less"] / nonties if nonties else float("nan")
            met = bool(nonties and share >= 0.8 and r["p_sign"] < 0.05)
            rows.append({"scope": label, "pair": f"cpu vs {other}", "pairs": f"{r['pairs']}/{expected}", "cpu_less": r["a_burns_less"], "cpu_more": r["a_burns_more"],
                         "ties": r["ties"], "share_less": share, "p_sign": r["p_sign"], "min_p_possible": sign_test_p(nonties, 0), "replica_diff": r["mean_replica_diff"],
                         "rule_met": met})
            verdicts.setdefault(label, []).append((met, r["pairs"], expected, r["a_burns_less"], nonties))
    lines = []
    for label, items in verdicts.items():
        complete = all(p >= e for _, p, e, _, _ in items)
        counts = " and ".join(f"{k}/{n} non-tied pairs vs {o}" for (_, _, _, k, n), o in zip(items, ("queue", "burn")))
        if label == fresh_label:
            minp = sign_test_p(items[0][4], 0)
            lines.append(f"H3 [{label}]: cpu burned less in {counts}"
                         + (" (direction replicated" if all(k == n and n > 0 for _, _, _, k, n in items) else " (direction NOT replicated in every pair")
                         + ("" if complete else "; INCOMPLETE") + ")"
                         + (f"; {items[0][4]} non-tied pairs cannot reach p < 0.05 (smallest possible p = {minp:.3f})" if minp >= 0.05 else
                            ("; significant at 0.05 on fresh pairs alone" if all(m for m, *_ in items) else "; not significant on fresh pairs alone")))
            continue
        state = ("CONFIRMED" if all(m for m, *_ in items) else "NOT CONFIRMED") + ("" if complete else " so far (INCOMPLETE: " + "/".join(f"{p} of {e}" for _, p, e, _, _ in items[:1]) + " pairs)")
        lines.append(f"H3 [{label}]: " + "; ".join(f"cpu < {o}: {k}/{n} non-tied" + (" ok" if m else " not met") for (m, _, _, k, n), o in zip(items, ("queue", "burn"))) + f" -> {state}")
    return pd.DataFrame(rows), lines


# ------------------------------------------------------------------------------------------------------------------ the report
def exclusion_sets(runs_dir=None) -> dict[str, set[tuple[str, str]]]:
    """(phase, run name) pairs to LEAVE OUT per variant: nothing; the runs a person kept after a re-run; the runs measured without host-logger samples.

    Pairs, not names: the same name exists in both phases (see gate.load_accepted)."""
    runs_dir = runs_dir or cfg.RUNS_DIR
    kept = set(gate.load_accepted(runs_dir.parent / gate.ACCEPT_NAME))
    unlogged = {(ph, r["name"]) for ph in ("A", "B") for r in gate.health_rows(runs_dir, ph) if r.get("busy") is None}
    return {"all": set(), "minus_accepted": kept, "host_logged_only": unlogged}


def _without(df: pd.DataFrame, pairs: set[tuple[str, str]]) -> pd.DataFrame:
    """Rows of an analysis table whose (phase, run) is not in `pairs`."""
    if df.empty or not pairs or "run" not in df or "phase" not in df:
        return df
    drop = pd.MultiIndex.from_frame(df[["phase", "run"]]).isin(list(pairs))
    return df[~drop].reset_index(drop=True)


def early_detections(det: pd.DataFrame, accepted: set[tuple[str, str]], max_ttd_s: float = 30.0, scenarios: tuple[str, ...] = (S7,)) -> pd.DataFrame:
    """Detections that fired within `max_ttd_s` of the incident start in a SLOW-onset scenario (default s7): there the incident cannot have burned
    enough budget yet, so a page that early comes from trouble BEFORE the incident (a bad stretch still burning when the incident begins), not from
    the incident. In fast-onset scenarios (s6 netem, s4 pod kill) a page within seconds is legitimate and is not listed. Check a listed run with
    `summarize --pages`."""
    if det.empty:
        return pd.DataFrame()
    d = det[det["has_incident"] & det["detected"].astype(bool) & (det["ttd_s"] <= max_ttd_s) & det["scenario"].isin(scenarios)]
    if d.empty:
        return pd.DataFrame()
    out = d[["scenario", "run", "detector", "ttd_s"]].copy()
    out["accepted"] = [("A", x) in accepted for x in out["run"]]
    return out.sort_values(["scenario", "run", "detector"]).reset_index(drop=True)


DIGEST_NAME = "ROBUSTNESS-digest.txt"
_KEY_H1 = ("tuned_class vs tuned_request", "tuned_token vs tuned_request")
_KEY_H2 = ("tuned_request vs google_x60_request",)


def _num(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.3f}" if isinstance(x, float) and abs(x) < 0.1 else (f"{x:.2f}" if isinstance(x, float) else str(x))


def _txt(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    """Plain-text table (no markdown pipes) narrow enough for one screenshot."""
    if df is None or df.empty:
        return "(none)"
    d = df[[c for c in cols if c in df.columns]] if cols else df
    return d.to_string(index=False, float_format=_num, na_rep="n/a")


def _pair_rows(full: pd.DataFrame, key_pairs: tuple[str, ...]) -> pd.DataFrame:
    """ALL / s7 / all-but-s7 for every pair, plus every scenario for the pairs the hypothesis is really about."""
    if full.empty:
        return full
    keep = full["scope"].isin(["ALL", S7, "all but s7"]) | full["pair"].isin(key_pairs)
    return full[keep].rename(columns={"ttd_a_minus_b_median_s": "ttd_diff_s"}).reset_index(drop=True)


def build_robust(include_invalid: bool = False) -> str:
    out = cfg.RUNS_DIR.parent
    out.mkdir(parents=True, exist_ok=True)
    a, b = analyze_phase("A", include_invalid), analyze_phase("B", include_invalid)
    ex = exclusion_sets()
    parts = ["# SLO-Loop Lab robustness and paired statistics\n"]
    dg = ["SLO-LOOP LAB DIGEST (screenshot this instead of attaching the zip if the attachment does not arrive)",
          "found = incident runs found / incident runs. rate has a Wilson 95% interval. a_only/b_only = runs only the first/second detector found; p_sign = exact sign test.",
          "ttd_diff_s < 0 = the first detector fired sooner. budget: 1.0 = SLO exactly met, 10 = ten times the allowed bad requests."]
    parts.append("Variants: `all` = every valid run; `minus_accepted` = without the runs kept after a re-run (`make accept-flagged`); "
                 "`host_logged_only` = only runs that have host-logger samples (Phase A only below). A conclusion should hold in all of them.\n")
    n_acc = len(ex["minus_accepted"])
    unlogged_a = {x for x in ex["host_logged_only"] if x[0] == "A"}
    parts.append(f"Left out: minus_accepted drops {n_acc} run(s); host_logged_only drops {len(unlogged_a)} Phase A run(s).\n")
    dg.append(f"left out: minus_accepted drops {n_acc} run(s) {sorted(ex['minus_accepted'])}; host_logged_only drops {len(unlogged_a)} Phase A run(s)")

    h4_tab, h4_lines = confirm_h4(a["rca"], ex["minus_accepted"])
    h3_tab, h3_lines = confirm_h3(b["summary"], ex["minus_accepted"])
    dg += ["", "== CONFIRMATION on fresh reps (rules fixed in docs/PREREGISTRATION-confirmation.md) =="] + h4_lines + h3_lines
    if not h4_tab.empty:
        dg += ["", "H4 fresh runs (naive = original estimator; class_adj and failure_tagged were designed after the first data):", _txt(h4_tab)]
        h4_tab.to_csv(out / "robust_confirm_h4.csv", index=False)
    if not h3_tab.empty:
        dg += ["", "H3 cpu policy against queue and burn (budget consumed, same traffic):", _txt(h3_tab, ["scope", "pair", "pairs", "cpu_less", "cpu_more", "ties", "share_less", "p_sign", "min_p_possible", "replica_diff", "rule_met"])]
        h3_tab.to_csv(out / "robust_confirm_h3.csv", index=False)
    parts.append("\n## Confirmation on fresh reps (pre-registered)\n\nRules: docs/PREREGISTRATION-confirmation.md.\n\n" + "\n".join(f"- {x}" for x in h4_lines + h3_lines) + "\n")
    if not h4_tab.empty:
        parts.append("\n" + md_table(h4_tab))
    if not h3_tab.empty:
        parts.append("\n" + md_table(h3_tab))

    # ---- Phase A
    det_all = a["detection"]
    parts.append("\n## Phase A - detectors on the same incident runs (variant: all)\n")
    if det_all.empty:
        parts.append("_no valid Phase A runs_\n")
    else:
        ds = detector_summary(det_all)
        ds.to_csv(out / "robust_detectors.csv", index=False)
        parts.append(md_table(ds))
        dg += ["", "== PHASE A: detectors, all incident runs ==", _txt(ds, ["detector", "found", "rate", "ci_lo", "ci_hi", "macro_rate", S7, "all_but_s7", "false_pages", "false_per_quiet_hr"])]
        parts.append("\n`rate` is pooled over incident runs (s7 has 13 reps, the others 6, so it weighs most); `macro_rate` gives every scenario the same weight; "
                     "`false_per_quiet_hr` divides all false pages by all non-incident time of all runs.\n")
        for title, pairs, name in (("H1 - token / class SLI against the request SLI (same thresholds)", SLI_PAIRS, "h1"),
                                   ("H2 - workbook thresholds (x60) against thresholds tuned for this system (same SLI)", THRESHOLD_PAIRS, "h2")):
            parts.append(f"\n## Phase A - {title}\n")
            full = paired_detection_rows(det_all, pairs, per_scenario=True)
            full.to_csv(out / f"robust_paired_{name}.csv", index=False)
            parts.append(md_table(full[full["scope"].isin(["ALL", S7, "all but s7"])].reset_index(drop=True)))
            dg += ["", f"== {title.split(' - ')[0]}: {title.split(' - ')[1]} ==",
                   _txt(_pair_rows(full, _KEY_H1 if name == "h1" else _KEY_H2), ["pair", "scope", "runs", "a_found", "b_found", "a_only", "b_only", "p_sign", "ttd_diff_s"])]
        parts.append("\n`a_only` / `b_only` = incident runs found by only the first / only the second detector; `p_sign` is the exact two-sided sign test on those; "
                     "`ttd_a_minus_b_median_s` < 0 means the first detector fired sooner (runs where both found it). Per-scenario rows are in the CSVs.\n")
        det_v = {"all": det_all, "minus_accepted": _without(det_all, ex["minus_accepted"]), "host_logged_only": _without(det_all, ex["host_logged_only"])}
        sens = sensitivity_detection(det_v)
        sens.to_csv(out / "robust_detectors_by_variant.csv", index=False)
        parts.append("\n## Phase A - do the detection rates depend on which runs are used?\n")
        parts.append(md_table(sens))
        dg += ["", "== PHASE A: found/incident runs by variant ==", _txt(sens)]
        early = early_detections(det_all, ex["minus_accepted"])
        if not early.empty:
            early.to_csv(out / "robust_early_detections.csv", index=False)
            parts.append("\n## Phase A - s7 slow burn: pages within 30 s of the incident start (check these)\n")
            parts.append(md_table(early))
            dg += ["", "== PHASE A: s7 slow burn, pages that fired within 30 s of the incident start (trouble BEFORE the incident, not the slow burn) ==",
                   _txt(early.head(40)) + (f"\n(+{len(early) - 40} more rows in robust_early_detections.csv)" if len(early) > 40 else "")]
            parts.append("\nA page that early usually comes from trouble before the incident, not from the incident. Check a run with "
                         "`.venv/bin/slo-lab summarize results/runs/A/<run> --pages`; `accepted=True` marks a run that was kept after a re-run.\n")
    rc = rca_summary(a["rca"])
    parts.append("\n## Phase A - H4 attribution with intervals (variant: all)\n")
    if rc.empty:
        parts.append("_no attribution rows_\n")
    else:
        rc.to_csv(out / "robust_rca.csv", index=False)
        parts.append(md_table(rc))
        dg += ["", "== PHASE A: H4 attribution (abstain = miss; chance = 0.33; tagged = exploratory) ==",
               _txt(rc, ["scenario", "truth", "incidents", "correct", "acc", "ci_lo", "ci_hi", "acc_if_answered", "always_llm", "exploratory_tagged"])]
        rca_minus = rca_summary(_without(a["rca"], ex["minus_accepted"]))
        ret = rca_minus[rca_minus["scenario"].str.startswith("RETRIEVER") | (rca_minus["truth"] == "retriever")] if not rca_minus.empty else rca_minus
        if not ret.empty:
            parts.append("\nSame, retriever incidents only, WITHOUT the accepted runs:\n\n" + md_table(ret.reset_index(drop=True)))
        if not ret.empty:
            dg += ["", "== PHASE A: H4, retriever incidents WITHOUT the accepted runs ==", _txt(ret.reset_index(drop=True), ["scenario", "incidents", "correct", "acc", "ci_lo", "ci_hi", "always_llm", "exploratory_tagged"])]
        parts.append("\n`acc` counts an abstention as a miss (as in `make analyze`); `acc_if_answered` does not. `chance` = 1/3. `exploratory_tagged` is the estimator that was designed "
                     "AFTER the Phase A data had been seen: do not quote it as a result, only as the reason to collect fresh reps.\n")

    # ---- Phase B
    sb = b["summary"]
    parts.append("\n## Phase B - budget consumed (x the budget; 1.0 = SLO exactly met), with and without the accepted runs\n")
    if sb.empty:
        parts.append("_no valid Phase B runs_\n")
    else:
        bv = budget_by_variant({"all": sb, "minus_accepted": _without(sb, ex["minus_accepted"])})
        bv.to_csv(out / "robust_budget_by_variant.csv", index=False)
        parts.append(md_table(bv))
        dg += ["", "== PHASE B: budget consumed and mean ready replicas, with and without the accepted runs ==", _txt(bv)]
        pb = paired_budget_rows(sb, POLICY_PAIRS, per_scenario=True)
        pb.to_csv(out / "robust_paired_budget.csv", index=False)
        parts.append("\n## Phase B - H3 policy against policy on the same traffic (all scenarios together)\n")
        parts.append(md_table(pb[pb["scope"] == "ALL"].reset_index(drop=True)))
        dg += ["", "== PHASE B: H3 policy against policy on the same traffic (all scenarios) ==",
               _txt(pb[pb["scope"] == "ALL"].reset_index(drop=True), ["pair", "pairs", "a_burns_less", "a_burns_more", "ties", "p_sign", "mean_budget_diff", "mean_replica_diff"])]
        pbm = paired_budget_rows(_without(sb, ex["minus_accepted"]), POLICY_PAIRS)
        if not pbm.empty and n_acc:
            parts.append("\nSame, WITHOUT the accepted runs:\n\n" + md_table(pbm))
        parts.append("\n`a_burns_less` = (scenario, rep) pairs where the first policy used less budget; ties (e.g. 0 against 0) are left out of the test. "
                     "`mean_replica_diff` is the price: the extra ready replicas the first policy ran on average. Per-scenario rows are in robust_paired_budget.csv.\n")

    parts.append("\n## Read this before quoting any number\n"
                 "- Exact sign tests on a handful of pairs: a result is only as strong as its smallest p and its weakest variant. No multiple-comparison correction.\n"
                 "- Same traffic does not mean independent samples: reps of one scenario share structure. Use these as evidence for the next experiment, not as a proof.\n"
                 "- A detector rate that moves a lot between variants is a result about the runs, not about the detector.\n")
    text = "\n".join(parts)
    (out / "ROBUSTNESS.md").write_text(text, encoding="utf-8")
    (out / DIGEST_NAME).write_text("\n".join(dg) + "\n", encoding="utf-8")
    return text


__all__ = ["build_robust", "wilson", "sign_test_p"]
