"""Stage-attribution root-cause baseline (H4).

Each successful request carries per-stage timings measured by the gateway. During an incident we
ask: which stage's median latency rose the most versus the pre-incident baseline? That stage's
component is the predicted root cause. This is deliberately simple (a span-less 'poor man's trace');
it is a baseline to be beaten, not a claim of state-of-the-art RCA. Requests that failed outright
carry no stage data, so a full outage yields an abstention rather than a guess.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

STAGE_COLUMNS = {
    "retriever": "retrieve_s",
    "llm_prefill": "llm_ttft_s",
    "llm_decode": "decode_s",
    "gateway_overhead": "overhead_s",
}
COMPONENT_OF = {
    "retriever": "retriever",
    "llm_prefill": "llm",
    "llm_decode": "llm",
    "gateway_overhead": "gateway",
}
COMPONENTS = ("retriever", "llm", "gateway")
MIN_ROWS = 5

# Which hop a FAILED request names. The gateway answers a retriever failure with HTTP 503, which the load generator
# records as "http_503" (the stream's own "retriever_error" never reaches the client); every other gateway status comes
# from the llm hop. Anything else (client_error, client_timeout, client_drop, ...) says nothing about a component.
FAILURE_COMPONENT = {
    "retriever_error": "retriever", "http_503": "retriever",
    "llm_error": "llm", "llm_incomplete": "llm", "timeout": "llm",
}


def failure_component(status) -> str | None:
    if status in FAILURE_COMPONENT:
        return FAILURE_COMPONENT[status]
    if isinstance(status, str) and status.startswith("llm_http_"):
        return "llm"
    return None


def with_stage_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["decode_s"] = out["total_s"] - out["ttft_s"]
    out["overhead_s"] = out["ttft_s"] - out["retrieve_s"] - out["llm_ttft_s"]
    return out[out["status"] == "ok"].dropna(subset=list(STAGE_COLUMNS.values()))


def localize(df: pd.DataFrame, baseline: tuple[float, float], incident: tuple[float, float]) -> dict:
    ok = with_stage_columns(df)
    b = ok[(ok["t_send"] >= baseline[0]) & (ok["t_send"] < baseline[1])]
    i = ok[(ok["t_send"] >= incident[0]) & (ok["t_send"] < incident[1])]
    if len(b) < MIN_ROWS or len(i) < MIN_ROWS:
        return {"pred": None, "scores": {}, "n_base": int(len(b)), "n_inc": int(len(i)), "abstain": True}
    scores = {c: 0.0 for c in COMPONENTS}
    for stage, col in STAGE_COLUMNS.items():
        delta = float(i[col].median() - b[col].median())
        scores[COMPONENT_OF[stage]] += max(delta, 0.0)
    pred = max(scores, key=scores.get) if any(v > 0 for v in scores.values()) else None
    return {"pred": pred, "scores": scores, "n_base": int(len(b)), "n_inc": int(len(i)), "abstain": pred is None}


def _abstain(n_base: int, n_inc: int, **extra) -> dict:
    return {"pred": None, "scores": {}, "n_base": n_base, "n_inc": n_inc, "abstain": True, **extra}


def localize_adjusted(df: pd.DataFrame, baseline: tuple[float, float], incident: tuple[float, float]) -> dict:
    """Like `localize`, but every request is compared with the baseline median of ITS OWN prompt class.

    The plain estimator compares pooled medians; with ~30 requests per window a few extra medium/long prompts by chance
    move a pooled median by seconds (a retriever-only fault was observed reporting llm=+7 s; the CAUSE of that +7 s is not yet verified, a
    prompt-mix shift alone is unlikely to reach 7 s, see DESIGN 14). Class-adjusting removes the
    prompt-mix noise. An incident request whose class has fewer than 2 baseline rows cannot be adjusted and is left out
    (falling back to the pooled median would bring the mix noise straight back); with fewer than MIN_ROWS adjustable
    incident requests the estimator abstains.
    """
    ok = with_stage_columns(df)
    b = ok[(ok["t_send"] >= baseline[0]) & (ok["t_send"] < baseline[1])]
    i = ok[(ok["t_send"] >= incident[0]) & (ok["t_send"] < incident[1])]
    if len(b) < MIN_ROWS or len(i) < MIN_ROWS:
        return _abstain(int(len(b)), int(len(i)))
    bcls = b["cls"] if "cls" in b.columns else pd.Series("all", index=b.index)
    icls = i["cls"] if "cls" in i.columns else pd.Series("all", index=i.index)
    usable = icls.map(bcls.value_counts()).fillna(0) >= 2
    i, icls = i[usable], icls[usable]
    if len(i) < MIN_ROWS:
        return _abstain(int(len(b)), int(len(i)))
    scores = {c: 0.0 for c in COMPONENTS}
    for stage, col in STAGE_COLUMNS.items():
        med = b.groupby(bcls)[col].median()
        delta = float((i[col] - icls.map(med)).median())
        scores[COMPONENT_OF[stage]] += max(delta, 0.0)
    pred = max(scores, key=scores.get) if any(v > 0 for v in scores.values()) else None
    return {"pred": pred, "scores": scores, "n_base": int(len(b)), "n_inc": int(len(i)), "abstain": pred is None}


def localize_tagged(df: pd.DataFrame, baseline: tuple[float, float], incident: tuple[float, float]) -> dict:
    """Failure evidence first, latency second.

    If the incident window contains failed requests that name a hop (see FAILURE_COMPONENT) at a higher rate than the
    baseline window does, the hop with the most such failures is the prediction (a tie falls through to latency).
    Otherwise this is `localize_adjusted`. Error-only faults (s6/s7, pod kills) carry no stage timings, which is why the
    latency-only estimators abstain or guess on them.
    """
    win = lambda lo, hi: df[(df["t_send"] >= lo) & (df["t_send"] < hi)]  # noqa: E731
    b, i = win(*baseline), win(*incident)
    comp_i = [c for c in (failure_component(s) for s in i["status"]) if c]
    comp_b = [c for c in (failure_component(s) for s in b["status"]) if c]
    rate_i = len(comp_i) / max(len(i), 1)
    rate_b = len(comp_b) / max(len(b), 1)
    if comp_i and rate_i > rate_b:
        counts = {c: float(comp_i.count(c)) for c in COMPONENTS}
        top = max(counts.values())
        winners = [c for c, v in counts.items() if v == top]
        if len(winners) == 1:
            return {"pred": winners[0], "scores": counts, "n_base": int(len(b)), "n_inc": int(len(i)), "abstain": False,
                    "evidence": "failures"}
    res = localize_adjusted(df, baseline, incident)
    res["evidence"] = "latency"
    return res


def score_methods(pred: str | None, truth: str) -> dict:
    """Compare the attribution method with two reference methods on one incident."""
    return {
        "attribution_correct": (pred == truth) if pred is not None else False,
        "attribution_abstained": pred is None,
        "always_llm_correct": truth == "llm",
        "random_expected": 1.0 / len(COMPONENTS),
    }
