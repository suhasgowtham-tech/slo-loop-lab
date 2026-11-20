"""Back-of-envelope model of the three KEDA/HPA triggers (cpu, queue, burn) -- a *prediction*, not a measurement.

Why this exists: a Phase B run costs ~12 minutes. Before spending 12 hours on 60 of them it is worth knowing whether a
trigger would already scale out at plain baseline load (then the "policy comparison" would only compare noise).

Model (deliberately simple, every assumption is a CLI flag):
  * Poisson arrivals at `rate` req/s, routed uniformly at random to the ready pods (kube-proxy per-connection balancing).
  * Each pod is a processor-sharing server: total speed `1 / service_s` jobs/s, split between the jobs in the pod.
    `service_s` = mean SOLO service time of one request (the "idle service" printed by `make latency`).
  * HPA: syncs every 15 s, tolerance 10 %, scale-up unlimited by stabilisation (0 s) and +3 pods / 15 s,
    scale-down = max recommendation over the last `down_window_s` (120 s) and -1 pod / 30 s. A new pod is ready after
    `pod_ready_s`. This is the documented algorithm of kube-controller-manager, re-implemented, not the real thing.
  * queue : desired = ceil(sum(inflight) / threshold)            (External metric, AverageValue)
  * burn  : desired = ceil(replicas * burn_30s / threshold)       (External metric, Value)
  * cpu   : desired = ceil(replicas * util / target), util = mean pod CPU / CPU request, CPU = `busy_cores` while busy
  * A request is BAD when its sojourn time exceeds `bad_after_s` or, independently, with probability `noise`
    (default 1 %; the measured baseline good-ratio of the lab is 98-99 %, i.e. NOT 100 %).
Known simplifications: exponential service times, pods do not slow each other down (a shared laptop CPU would make
extra replicas LESS useful than modelled), instantaneous sampling of the metric (real: up to 5 s scrape delay).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

HPA_SYNC_S = 15.0
TOLERANCE = 0.10
DT = 0.25


@dataclass
class Params:
    policy: str = "queue"            # queue | burn | cpu | static1
    service_s: float = 2.93           # mean solo service time (s)
    base_rps: float = 0.178
    duration_s: float = 600.0
    burst_start_s: float | None = None
    burst_end_s: float | None = None
    burst_x: float = 3.0
    max_replicas: int = 3
    min_replicas: int = 1
    threshold: float = 2.0            # queue: inflight per replica; burn: burn rate; cpu: % of request
    cpu_util_target: float = 60.0
    cpu_request_m: float = 1000.0
    busy_cores: float = 1.8           # cores used by llama.cpp while it is generating (2 threads)
    pod_ready_s: float = 6.0
    down_window_s: float = 120.0
    bad_after_s: float = 12.0         # sojourn above this = bad (use ~1.5x the calibrated short/medium threshold)
    noise: float = 0.01
    slo_target: float = 0.99
    burn_window_s: float = 30.0
    burn_min_requests: int = 0        # >0 = ignore the burn signal unless this many requests finished in the window
    seed: int = 0


@dataclass
class Result:
    t: np.ndarray
    ready: np.ndarray
    desired: np.ndarray
    inflight: np.ndarray
    burn: np.ndarray
    events: list = field(default_factory=list)  # (t, spec, ready) change points
    sojourns: np.ndarray = field(default_factory=lambda: np.array([]))  # per finished request, seconds

    @property
    def mean_ready(self) -> float:
        return float(self.ready.mean())

    def frac_at(self, n: int) -> float:
        return float((self.ready == n).mean())

    def first_scale_out(self, after: float = 0.0) -> float | None:
        for t, r in zip(self.t, self.ready):
            if t >= after and r >= 2:
                return float(t - after)
        return None


def _rate(p: Params, t: float) -> float:
    if p.burst_start_s is not None and p.burst_start_s <= t < (p.burst_end_s or p.duration_s):
        return p.base_rps * p.burst_x
    return p.base_rps


def simulate(p: Params) -> Result:
    rng = np.random.default_rng(p.seed)
    starting: list[float] = []                # ready-at times of pods being started
    spec = p.min_replicas
    pods: list[list[list[float]]] = [[] for _ in range(spec)]   # per READY pod: jobs as [remaining solo-seconds, t_arrival]
    n_steps = int(p.duration_s / DT)
    t_arr, ready_arr, des_arr, infl_arr, burn_arr = [], [], [], [], []
    done: list[tuple[float, bool]] = []       # (t_done, bad)
    sojourns: list[float] = []
    busy_hist: list[float] = []               # per step: mean busy fraction over ready pods
    recs: list[tuple[float, int]] = []
    next_sync = HPA_SYNC_S
    last_down = -1e9
    events: list[tuple[float, int, int]] = []
    prev = None
    burn_now = 0.0
    next_rule = 0.0

    for step in range(n_steps):
        t = step * DT
        # pods that finished starting
        while starting and starting[0] <= t:
            starting.pop(0)
            pods.append([])
        # arrivals
        for _ in range(rng.poisson(_rate(p, t) * DT)):
            k = int(rng.integers(len(pods)))
            pods[k].append([float(rng.exponential(p.service_s)), t])
        # service (processor sharing: each of n jobs gets 1/n of the solo speed)
        busy = 0
        for k, jobs in enumerate(pods):
            n = len(jobs)
            if not n:
                continue
            busy += 1
            share = DT / n
            for j in range(n - 1, -1, -1):
                jobs[j][0] -= share
                if jobs[j][0] <= 0:
                    soj = t - jobs[j][1] + DT
                    bad = soj > p.bad_after_s or rng.random() < p.noise
                    done.append((t, bool(bad)))
                    sojourns.append(soj)
                    jobs.pop(j)
        busy_hist.append(busy / max(len(pods), 1))
        inflight = sum(len(j) for j in pods)

        # recording rule (every 5 s): burn over the last window
        if t >= next_rule:
            next_rule += 5.0
            lo = t - p.burn_window_s
            win = [b for td, b in done[-400:] if td > lo]
            if win and len(win) >= max(1, p.burn_min_requests):
                burn_now = (sum(win) / len(win)) / (1.0 - p.slo_target)
            else:
                burn_now = 0.0

        # HPA
        if t >= next_sync and p.policy != "static1":
            next_sync += HPA_SYNC_S
            cur = len(pods) + len(starting)
            ready_n = max(len(pods), 1)
            if p.policy == "queue":
                ratio = inflight / (p.threshold * cur)
                desired = math.ceil(inflight / p.threshold) if abs(1 - ratio) > TOLERANCE else cur
            elif p.policy == "burn":
                ratio = burn_now / p.threshold
                desired = math.ceil(ratio * ready_n) if abs(1 - ratio) > TOLERANCE else cur
            elif p.policy == "cpu":
                window = int(HPA_SYNC_S / DT)
                frac = float(np.mean(busy_hist[-window:]))
                util = 100.0 * frac * p.busy_cores * 1000.0 / p.cpu_request_m
                ratio = util / p.cpu_util_target
                desired = math.ceil(ratio * ready_n) if abs(1 - ratio) > TOLERANCE else cur
            else:
                raise ValueError(p.policy)
            desired = max(p.min_replicas, min(p.max_replicas, desired))
            recs.append((t, desired))
            recs = [(a, b) for a, b in recs if a > t - p.down_window_s]
            final = cur
            if desired > cur:
                final = min(desired, cur + 3)
            elif desired < cur:
                final = max(max(b for _, b in recs), desired)
                if final < cur and t - last_down >= 30.0:
                    final = cur - 1
                    last_down = t
                else:
                    final = cur
            if final > cur:
                for _ in range(final - cur):
                    starting.append(t + p.pod_ready_s)
                spec = final
            elif final < cur:
                spec = final
                if starting and final < len(pods) + len(starting):
                    starting.pop()
                else:
                    # remove the pod with the fewest jobs; re-route its jobs
                    k = min(range(len(pods)), key=lambda i: len(pods[i]))
                    orphans = pods.pop(k)
                    for job in orphans:
                        pods[int(rng.integers(len(pods)))].append(job)
            des_now = spec
        else:
            des_now = spec
        cur_state = (des_now, len(pods))
        if cur_state != prev:
            events.append((t, des_now, len(pods)))
            prev = cur_state
        if step % int(1.0 / DT) == 0:
            t_arr.append(t)
            ready_arr.append(len(pods))
            des_arr.append(des_now)
            infl_arr.append(inflight)
            burn_arr.append(burn_now)
    return Result(np.array(t_arr), np.array(ready_arr), np.array(des_arr), np.array(infl_arr), np.array(burn_arr), events, np.array(sojourns))


def monte_carlo(p: Params, reps: int = 100) -> dict:
    rs = [simulate(Params(**{**p.__dict__, "seed": p.seed + i})) for i in range(reps)]
    after = p.burst_start_s
    so = [r.first_scale_out(after or 0.0) for r in rs]
    out = {
        "policy": p.policy,
        "reps": reps,
        "mean_ready_replicas": float(np.mean([r.mean_ready for r in rs])),
        "frac_time_at_1_replica": float(np.mean([r.frac_at(1) for r in rs])),
        "frac_time_at_3_replicas": float(np.mean([r.frac_at(3) for r in rs])),
        "runs_that_ever_scale_out": float(np.mean([r.ready.max() >= 2 for r in rs])),
    }
    if after is not None:
        got = [x for x in so if x is not None]
        out["scale_out_after_burst_s_median"] = float(np.median(got)) if got else None
    else:
        out["scale_out_after_start_s_median"] = float(np.median([x for x in so if x is not None])) if any(x is not None for x in so) else None
    return out


def calibrate_bad_after(p: Params, quantile: float = 0.99, duration_s: float = 20000.0) -> float:
    """Sojourn time that a single static replica exceeds with probability 1-quantile at the baseline load.

    Mirrors what `make calibrate` does (threshold derived from a clean baseline), so that the simulated healthy state
    has ~1 % bad requests instead of an arbitrary number."""
    r = simulate(Params(**{**p.__dict__, "policy": "static1", "duration_s": duration_s, "burst_start_s": None, "noise": 0.0}))
    return float(np.quantile(r.sojourns, quantile))
