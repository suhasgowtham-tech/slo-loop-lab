"""Open-loop load generator.

Why open-loop: a closed-loop client (N workers, each waits for its reply) slows down exactly when
the system slows down, which hides queueing delay ("coordinated omission"). Here arrivals follow a
pre-computed non-homogeneous Poisson schedule and are sent at their scheduled instant regardless of
how many requests are still in flight. The per-request log is the ground-truth dataset; detectors
and SLIs are evaluated on it offline.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

CLASS_DEFAULTS = {
    "short": {"words": 50, "max_tokens": 64},
    "medium": {"words": 400, "max_tokens": 64},
    "long": {"words": 1200, "max_tokens": 64},
}

WORDS = (
    "system service request latency queue token model cache network cluster node pod deploy rollout "
    "metric alert budget error burn rate window traffic load balance gateway backend upstream timeout "
    "retry circuit breaker memory cpu disk storage volume config secret policy scale replica probe "
    "health ready stable fault chaos inject recover restore incident review capacity forecast trend "
    "signal noise sample quantile bucket counter gauge series label scrape target rule record query "
    "trace span stage batch stream prompt context answer answer response write read update delete "
    "create list watch patch apply render template value release chart version lock pin upgrade "
    "rollback canary shadow mirror route proxy mesh ingress egress limit quota priority fairness"
).split()

CSV_FIELDS = [
    "idx", "cls", "t_sched", "t_send", "t_done", "weight", "status", "ttft_s", "total_s",
    "retrieve_s", "llm_ttft_s", "prompt_tokens", "completion_tokens",
]


@dataclass(frozen=True)
class Arrival:
    idx: int
    t_sched: float
    cls: str


def weight_of(prompt: str, max_tokens: int) -> int:
    """Cost proxy known at admission time. MUST match apps/gateway/app.py:weight_of (tested)."""
    return math.ceil(len(prompt) / 4) + max_tokens


def build_arrivals(segments: list[dict], duration_s: float, base_rps: float, seed: int) -> list[Arrival]:
    """Piecewise-constant-rate Poisson arrivals.

    segments: [{at, rps_x, mix?}, ...] sorted by `at`. rate = rps_x * base_rps. `mix` (class
    probabilities) is inherited from the previous segment when omitted.
    """
    if not segments:
        raise ValueError("scenario has no load segments")
    segs = sorted(segments, key=lambda s: s["at"])
    if segs[0]["at"] != 0:
        raise ValueError("first load segment must start at t=0")
    rng = random.Random(seed)
    arrivals: list[Arrival] = []
    mix = None
    for i, seg in enumerate(segs):
        mix = seg.get("mix", mix)
        if mix is None:
            raise ValueError("first load segment must define `mix`")
        classes = list(mix.keys())
        probs = [float(mix[c]) for c in classes]
        if abs(sum(probs) - 1.0) > 1e-6:
            raise ValueError(f"mix must sum to 1, got {sum(probs)}")
        start = float(seg["at"])
        end = float(segs[i + 1]["at"]) if i + 1 < len(segs) else float(duration_s)
        rate = float(seg["rps_x"]) * base_rps
        if rate <= 0:
            continue
        t = start
        while True:
            t += rng.expovariate(rate)  # memoryless: restarting at a boundary is statistically exact
            if t >= end:
                break
            arrivals.append(Arrival(idx=len(arrivals), t_sched=t, cls=rng.choices(classes, probs)[0]))
    return arrivals


def make_prompt(words: int, rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def prepare_bodies(arrivals: Iterable[Arrival], classes: dict, seed: int) -> list[dict]:
    bodies = []
    for a in arrivals:
        cfg = classes[a.cls]
        rng = random.Random(f"{seed}:{a.idx}")  # unique & deterministic per request
        prompt = make_prompt(int(cfg["words"]), rng)
        bodies.append({"prompt": prompt, "max_tokens": int(cfg["max_tokens"]), "prompt_class": a.cls})
    return bodies


@dataclass
class LoadResult:
    rows: list[dict]
    max_lag_s: float
    n_dropped: int
    start_epoch: float
    n_scheduled: int = 0  # arrivals the schedule contained; len(rows) must equal it (every request is accounted for)


async def _one(client: httpx.AsyncClient, a: Arrival, body: dict, t0: float, timeout_s: float) -> dict:
    t_send = time.perf_counter()
    row = {
        "idx": a.idx, "cls": a.cls, "t_sched": a.t_sched, "t_send": t_send - t0,
        "weight": weight_of(body["prompt"], body["max_tokens"]),
        "status": "client_error", "ttft_s": None, "total_s": None, "retrieve_s": None,
        "llm_ttft_s": None, "prompt_tokens": None, "completion_tokens": None,
    }
    async def call() -> None:
        async with client.stream("POST", "/v1/generate", json=body) as r:
            if r.status_code != 200:
                row["status"] = f"http_{r.status_code}"
                return
            done = None
            async for line in r.aiter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                kind = msg.get("type")
                if kind == "token":
                    if row["ttft_s"] is None:
                        row["ttft_s"] = time.perf_counter() - t_send
                elif kind == "done":
                    done = msg
            if done is None:
                row["status"] = "incomplete"
            else:
                row["status"] = done.get("status", "unknown")
                for k in ("retrieve_s", "llm_ttft_s", "prompt_tokens", "completion_tokens"):
                    row[k] = done.get(k)

    try:
        await asyncio.wait_for(call(), timeout_s)  # wait_for (not asyncio.timeout) => works on Python 3.10
    except (asyncio.TimeoutError, TimeoutError):
        row["status"] = "client_timeout"
    except Exception:  # noqa: BLE001  any failure of THIS request is data (a bad event), never a reason to lose the row
        row["status"] = "client_error"
    finally:
        row["total_s"] = time.perf_counter() - t_send
        row["t_done"] = row["t_send"] + row["total_s"]
    return row


async def run_load(
    gateway_url: str,
    arrivals: list[Arrival],
    bodies: list[dict],
    *,
    request_timeout_s: float = 90.0,
    max_outstanding: int = 400,
    start_epoch: float | None = None,
) -> LoadResult:
    if start_epoch is None:
        start_epoch = time.time() + 0.5
    t0 = time.perf_counter() + (start_epoch - time.time())
    rows: list[dict] = []
    outstanding = 0
    max_lag = 0.0
    dropped = 0
    tasks: set[asyncio.Task] = set()
    info: dict[asyncio.Task, tuple[Arrival, dict]] = {}

    def _done(task: asyncio.Task) -> None:
        nonlocal outstanding
        outstanding -= 1
        tasks.discard(task)
        a_, body_ = info.pop(task, (None, None))
        if task.cancelled():
            return
        if task.exception() is None:
            rows.append(task.result())
        elif a_ is not None:  # defence in depth: an unexpected crash must still leave a failed request in the data
            rows.append({
                "idx": a_.idx, "cls": a_.cls, "t_sched": a_.t_sched, "t_send": a_.t_sched, "t_done": a_.t_sched,
                "weight": weight_of(body_["prompt"], body_["max_tokens"]), "status": "client_error", "ttft_s": None,
                "total_s": None, "retrieve_s": None, "llm_ttft_s": None, "prompt_tokens": None, "completion_tokens": None,
            })

    limits = httpx.Limits(max_connections=max_outstanding + 10, max_keepalive_connections=0)
    async with httpx.AsyncClient(base_url=gateway_url, limits=limits, timeout=httpx.Timeout(request_timeout_s)) as client:
        for a, body in zip(arrivals, bodies):
            delay = t0 + a.t_sched - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            max_lag = max(max_lag, time.perf_counter() - (t0 + a.t_sched))
            if outstanding >= max_outstanding:
                dropped += 1
                rows.append({
                    "idx": a.idx, "cls": a.cls, "t_sched": a.t_sched, "t_send": a.t_sched,
                    "t_done": a.t_sched, "weight": weight_of(body["prompt"], body["max_tokens"]),
                    "status": "client_drop", "ttft_s": None, "total_s": 0.0, "retrieve_s": None,
                    "llm_ttft_s": None, "prompt_tokens": None, "completion_tokens": None,
                })
                continue
            outstanding += 1
            task = asyncio.create_task(_one(client, a, body, t0, request_timeout_s))
            tasks.add(task)
            info[task] = (a, body)
            task.add_done_callback(_done)
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
    rows.sort(key=lambda r: r["idx"])
    return LoadResult(rows=rows, max_lag_s=max_lag, n_dropped=dropped, start_epoch=start_epoch, n_scheduled=len(arrivals))


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
