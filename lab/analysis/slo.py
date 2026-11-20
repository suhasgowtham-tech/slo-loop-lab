"""SLI definitions and windowed burn-rate math, computed offline from the client-side request log.

Events are timestamped at COMPLETION (t_done): an alerting system can only react to outcomes that
have been observed, so a request that hangs for 60 s is invisible until it times out. That is a
real property of SLO alerting and is deliberately preserved here.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

CLASSES = ("short", "medium", "long")
NUMERIC = ["t_sched", "t_send", "t_done", "weight", "ttft_s", "total_s", "retrieve_s",
           "llm_ttft_s", "prompt_tokens", "completion_tokens"]


def load_requests(path: Path | str) -> pd.DataFrame:
    df = pd.read_csv(path)
    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "t_done" not in df.columns or df["t_done"].isna().all():
        df["t_done"] = df["t_send"] + df["total_s"].fillna(0)
    return df.sort_values("t_done").reset_index(drop=True)


def annotate(df: pd.DataFrame, ttft_thr: dict[str, float]) -> pd.DataFrame:
    """Add boolean `good`: status ok AND TTFT within the class threshold."""
    out = df.copy()
    thr = out["cls"].map(ttft_thr)
    out["good"] = (out["status"] == "ok") & out["ttft_s"].notna() & (out["ttft_s"] <= thr)
    return out.sort_values("t_done").reset_index(drop=True)


def _window_sums(t_sorted: np.ndarray, values: np.ndarray, grid: np.ndarray, window_s: float) -> np.ndarray:
    cum = np.concatenate([[0.0], np.cumsum(values, dtype=float)])
    hi = np.searchsorted(t_sorted, grid, side="right")
    lo = np.searchsorted(t_sorted, grid - window_s, side="right")
    return cum[hi] - cum[lo]


def _bad_ratio(df: pd.DataFrame, grid: np.ndarray, window_s: float, weighted: bool, min_events: int) -> np.ndarray:
    t = df["t_done"].to_numpy(dtype=float)
    bad = (~df["good"]).to_numpy()
    w = df["weight"].to_numpy(dtype=float) if weighted else np.ones(len(df))
    n = _window_sums(t, np.ones(len(df)), grid, window_s)
    tot = _window_sums(t, w, grid, window_s)
    badw = _window_sums(t, w * bad, grid, window_s)
    ratio = np.divide(badw, tot, out=np.zeros_like(badw), where=tot > 0)
    ratio[n < min_events] = 0.0  # not enough evidence in the window
    return ratio


def burn_series(
    df: pd.DataFrame, window_s: float, grid: np.ndarray, sli: str, target: float, min_events: int = 5
) -> np.ndarray:
    """Burn rate = (bad fraction in window) / (1 - target), sampled on `grid`.

    sli: "request" (count events), "token" (events weighted by cost proxy),
         "class" (per prompt-class request SLI; the burn is the worst class).
    """
    budget = 1.0 - target
    if budget <= 0:
        raise ValueError("target must be < 1")
    if len(df) == 0:
        return np.zeros(len(grid))
    if sli == "request":
        return _bad_ratio(df, grid, window_s, False, min_events) / budget
    if sli == "token":
        return _bad_ratio(df, grid, window_s, True, min_events) / budget
    if sli == "class":
        per = []
        for c in CLASSES:
            sub = df[df["cls"] == c]
            per.append(_bad_ratio(sub, grid, window_s, False, min_events) if len(sub) else np.zeros(len(grid)))
        return np.max(per, axis=0) / budget
    raise ValueError(f"unknown sli {sli!r}")


def budget_consumed(df: pd.DataFrame, target: float, t_from: float, t_to: float, sli: str = "request") -> float:
    """Whole-window burn: >1 means the SLO was missed over [t_from, t_to]."""
    sub = df[(df["t_done"] > t_from) & (df["t_done"] <= t_to)]
    if len(sub) == 0:
        return 0.0
    if sli == "token":
        w = sub["weight"].to_numpy(dtype=float)
        ratio = float((w * (~sub["good"]).to_numpy()).sum() / w.sum())
    else:
        ratio = float((~sub["good"]).mean())
    return ratio / (1.0 - target)


def class_stats(df: pd.DataFrame, t_from: float = 0.0, t_to: float = float("inf")) -> dict:
    """p50/p95 TTFT (successful requests), error ratio and good ratio per class + overall."""
    sub = df[(df["t_send"] >= t_from) & (df["t_send"] < t_to)]
    out: dict = {}
    for name, part in [(c, sub[sub["cls"] == c]) for c in CLASSES] + [("all", sub)]:
        ok = part[(part["status"] == "ok") & part["ttft_s"].notna()]
        out[name] = {
            "n": int(len(part)),
            "ttft_p50": float(ok["ttft_s"].quantile(0.5)) if len(ok) else None,
            "ttft_p95": float(ok["ttft_s"].quantile(0.95)) if len(ok) else None,
            "error_ratio": float((part["status"] != "ok").mean()) if len(part) else None,
            "good_ratio": float(part["good"].mean()) if len(part) and "good" in part else None,
        }
    return out
