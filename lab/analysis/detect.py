"""Multi-window multi-burn-rate (MWMBR) detectors, replayed offline on a request log.

A rule fires when BOTH its long and short window burn rates are >= the rule's burn threshold
(Google SRE Workbook, ch. 5). A detector fires when ANY of its rules fire. Because detection is an
offline replay, many detector configurations can be compared on exactly the same traffic.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .slo import burn_series


@dataclass(frozen=True)
class Rule:
    long_s: float
    short_s: float
    burn: float


@dataclass(frozen=True)
class Detector:
    name: str
    sli: str  # request | token | class
    rules: tuple[Rule, ...]
    min_events: int = 5


def detector_from_dict(d: dict) -> Detector:
    return Detector(
        name=d["name"],
        sli=d["sli"],
        rules=tuple(Rule(float(r["long_s"]), float(r["short_s"]), float(r["burn"])) for r in d["rules"]),
        min_events=int(d.get("min_events", 5)),
    )


def scale_detector(det: Detector, factor: float, max_long_s: float | None = None) -> tuple[Detector, list[Rule]]:
    """Stretch every window by `factor` (long and short alike, so each rule's long:short ratio is unchanged).

    Rules whose stretched long window would not fit in the run (> max_long_s) are dropped and returned, so the
    caller can report that they could not be evaluated instead of silently scoring a truncated window.
    """
    kept, dropped = [], []
    for r in det.rules:
        nr = Rule(r.long_s * factor, r.short_s * factor, r.burn)
        (dropped if max_long_s is not None and nr.long_s > max_long_s else kept).append(nr)
    return Detector(det.name, det.sli, tuple(kept), det.min_events), dropped


def auto_time_scale(n_requests: int, duration_s: float, det: Detector, scales=(1.0, 2.0, 3.0, 4.0, 6.0),
                    max_long_frac: float = 0.8) -> float:
    """Smallest stretch for which `det`'s shortest short window is expected to hold >= det.min_events events.

    Low-traffic alerting problem (SRE Workbook, ch. 5): at a few requests/minute a 5 s window holds ~0-1 events, so a
    multi-window rule can never be armed. Stretching a detector's windows equally keeps its burn-rate logic identical and
    only lowers the time resolution. Capped so that the detector's SHORTEST long window still fits in `max_long_frac` of
    the run (longer rules are then dropped by `scale_detector` and reported, never truncated).
    """
    rate = n_requests / max(duration_s, 1e-9)
    shortest = min(r.short_s for r in det.rules)
    smallest_long = min(r.long_s for r in det.rules)
    best = scales[0]
    for sc in scales:
        if sc > scales[0] and smallest_long * sc > max_long_frac * duration_s:
            break
        best = sc
        if rate * shortest * sc >= det.min_events:
            break
    return float(best)


def evaluate(df: pd.DataFrame, det: Detector, target: float, t_end: float, step_s: float = 1.0,
             min_history_s: float = 60.0):
    """Return (grid, fire) where fire[i] is True if the detector is alerting at time grid[i].

    A run starts with an empty history, so the first few events would make any window look wildly
    unhealthy (1 bad request out of 12 = 8% bad). A production alerter always has history; to avoid
    start-up false pages in the replay, each rule stays silent until min(long window, min_history_s)
    seconds of the run have been observed. Documented as a threat to validity in docs/DESIGN.md.
    """
    grid = np.arange(step_s, t_end + step_s * 0.5, step_s)
    fire = np.zeros(len(grid), dtype=bool)
    for r in det.rules:
        bl = burn_series(df, r.long_s, grid, det.sli, target, det.min_events)
        bs = burn_series(df, r.short_s, grid, det.sli, target, det.min_events)
        armed = grid >= min(r.long_s, min_history_s)
        fire |= (bl >= r.burn) & (bs >= r.burn) & armed
    return grid, fire


def alert_intervals(grid: np.ndarray, fire: np.ndarray) -> list[tuple[float, float]]:
    """Contiguous runs of alerting state -> [(start, end), ...] (each run is one 'page')."""
    out: list[tuple[float, float]] = []
    start = None
    prev = None
    for t, f in zip(grid, fire):
        if f and start is None:
            start = float(t)
        if not f and start is not None:
            out.append((start, float(prev)))
            start = None
        prev = t
    if start is not None:
        out.append((start, float(prev)))
    return out


def score(
    intervals: list[tuple[float, float]], incidents: list[dict], run_end: float, grace_s: float = 60.0
) -> dict:
    """Score alert intervals against ground-truth incidents.

    A page is TRUE if it starts inside [incident.start, incident.end + grace]; otherwise FALSE.
    ttd_s is for the first incident (scenarios define at most one). False pages are normalised by
    the amount of non-incident time in the run.
    """
    windows = [(float(i["start"]), float(i["end"]) + grace_s) for i in incidents]
    true_pages = []
    false_pages = []
    for s, e in intervals:
        if any(ws <= s <= we for ws, we in windows):
            true_pages.append((s, e))
        else:
            false_pages.append((s, e))
    ttd = None
    detected = False
    if incidents:
        ws, we = windows[0]
        firsts = [s for s, _ in true_pages if ws <= s <= we]
        if firsts:
            detected = True
            ttd = max(0.0, min(firsts) - float(incidents[0]["start"]))
    covered = 0.0
    last_end = 0.0
    for ws, we in sorted(windows):
        ws = max(ws, last_end)
        we = min(we, run_end)
        if we > ws:
            covered += we - ws
            last_end = we
    quiet_s = max(0.0, run_end - covered)
    return {
        "has_incident": bool(incidents),
        "detected": detected,
        "ttd_s": ttd,
        "pages_total": len(intervals),
        "false_pages": len(false_pages),
        "quiet_hours": quiet_s / 3600.0,
        "false_pages_per_hour": (len(false_pages) / (quiet_s / 3600.0)) if quiet_s > 0 else None,
    }
