"""Latency + capacity probes and SLO calibration. All three make the lab adapt to the machine it runs on.

Order matters and is enforced by the Makefile: `make latency` (what does ONE request cost here?) ->
`make capacity` (how many per second can one replica serve?) -> `make calibrate` (what is "good" TTFT here?).

Why the capacity probe starts from the idle latency instead of a fixed rate: on a laptop CPU the cost of a
request varies by 10x with the workload profile. A fixed first step (the original 0.2 rps) overloaded the
service immediately, and a probe that can only climb has nowhere to go. The queueing-theory first guess is
capacity ~= 1 / mean_service_time; we start well below it, climb until the service degrades, and if even the
first step degrades we retreat downward instead of giving up.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import config as cfg
from . import k8s, loadgen, runner

CLASSES = ("short", "medium", "long")


def _one_request(load_cfg: dict, cls: str, seed: int) -> dict:
    """One request, nothing else in flight: the 'idle' cost of a request of this class."""
    arr = [loadgen.Arrival(idx=0, t_sched=0.0, cls=cls)]
    bodies = loadgen.prepare_bodies(arr, load_cfg["classes"], seed)
    client = load_cfg.get("client", {})
    res = asyncio.run(loadgen.run_load(
        cfg.GATEWAY_URL, arr, bodies,
        request_timeout_s=float(client.get("request_timeout_s", 90)), max_outstanding=4,
        start_epoch=time.time() + 0.05,
    ))
    return res.rows[0]


def _load1() -> float | None:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError):
        return None


def quiet_limit() -> float:
    """1-minute load average below which the machine counts as quiet: 15 % of the logical CPUs, at least 1.0.
    (The llm pod itself uses 2 threads, so a background load of ~2 already halves the CPU it can use.)"""
    return max(1.0, 0.15 * (os.cpu_count() or 1))


def wait_until_quiet(max_wait_s: float | None = None, poll_s: float = 15.0) -> float | None:
    """Wait, bounded, for the machine to calm down before a measurement; never fatal.

    Capacity, idle latency and the SLO thresholds are only meaningful if the machine is as quiet then as it will be
    during the experiments. Returns the final 1-minute load average (None when /proc/loadavg is unreadable).
    LAB_QUIET_WAIT_S overrides the 300 s default (0 = do not wait; the test-suite uses that).
    """
    if max_wait_s is None:
        max_wait_s = float(os.getenv("LAB_QUIET_WAIT_S", "300"))
    limit = quiet_limit()
    cur = _load1()
    if cur is None:
        return None
    if cur <= limit:
        print(f"[quiet] load average {cur:.2f} (limit {limit:.1f}): the machine is quiet")
        return cur
    print(f"[quiet] load average {cur:.2f} is above {limit:.1f}: waiting up to {max_wait_s:.0f} s for the machine to calm down "
          "(who is using the CPU?  docker stats --no-stream)")
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        time.sleep(poll_s)
        cur = _load1()
        if cur is None or cur <= limit:
            print(f"[quiet] load average {cur if cur is None else round(cur, 2)}: quiet enough, continuing")
            return cur
    print(f"[quiet] WARNING: still {cur:.2f} after waiting. Measuring anyway, but the numbers will be pessimistic and noisy: "
          "stop other containers (docker ps), close Windows apps, plug in the charger, Windows power mode 'Best performance'.")
    return cur


def host_notes() -> list[str]:
    """Things on the machine that make every latency number unreliable. Printed, never fatal."""
    notes = []
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
        ncpu = os.cpu_count() or 1
        if load1 > 0.25 * ncpu:
            notes.append(f"load average is {load1:.1f} on {ncpu} CPUs before the probe starts: close other programs "
                         "(browsers, IDEs, Docker Desktop's built-in Kubernetes) for trustworthy numbers")
    except (OSError, ValueError):
        pass
    return notes


def measure_latency(per_class: int = 4, warmup: int = 3, seed: int = 9100, save: bool = True) -> dict:
    """Sequential idle measurement: `warmup` discarded requests, then `per_class` of each class, round-robin."""
    load_cfg = cfg.load_load()
    wait_until_quiet()
    print(f"[latency] profile={load_cfg.get('profile', '?')}: {warmup} warm-up requests, then {per_class} per class, one at a time")
    for i in range(warmup):
        _one_request(load_cfg, CLASSES[i % 3], seed + i)
    rows = []
    for rnd in range(per_class):
        for j, c in enumerate(CLASSES):
            r = _one_request(load_cfg, c, seed + 1000 + rnd * 10 + j)
            rows.append(r)
            print(f"[latency]   {c:6s} status={r['status']:<10s} ttft={_fmt(r['ttft_s'])} total={_fmt(r['total_s'])}")
    df = pd.DataFrame(rows)
    per = {}
    for c in CLASSES:
        ok = df[(df["cls"] == c) & (df["status"] == "ok")]
        if ok.empty:
            seen = df[df["cls"] == c]["status"].value_counts().to_dict()
            raise RuntimeError(f"no successful '{c}' request while idle (statuses: {seen}). The lab itself is unhealthy: "
                               "run `make verify` and look at `kubectl -n slo-loop logs deploy/llm --tail=30`.")
        per[c] = {"n_ok": int(len(ok)), "ttft_mean_s": round(float(ok["ttft_s"].mean()), 3),
                  "total_mean_s": round(float(ok["total_s"].mean()), 3), "total_max_s": round(float(ok["total_s"].max()), 3)}
    mix = load_cfg["default_mix"]
    s_mix = sum(float(mix[c]) * per[c]["total_mean_s"] for c in CLASSES)
    est_cap = 1.0 / s_mix
    ttft_p95 = float(np.quantile(df[df["status"] == "ok"]["ttft_s"].dropna(), 0.95))
    out = {"per_class": per, "mean_service_s": round(s_mix, 3), "est_capacity_rps": round(est_cap, 3),
           "idle_ttft_p95_s": round(ttft_p95, 3), "profile": load_cfg.get("profile"),
           "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print("\nclass     n   idle TTFT   idle total")
    for c in CLASSES:
        p = per[c]
        print(f"{c:8s} {p['n_ok']:3d}   {p['ttft_mean_s']:7.2f} s   {p['total_mean_s']:7.2f} s")
    print(f"\n[latency] mean service time for the default mix: {s_mix:.2f} s  ->  one replica can serve roughly {est_cap:.2f} requests/s")
    if s_mix > 6.0:
        print("[latency] WARNING: that is a heavy workload for this machine (a 10-minute run would hold only ~"
              f"{int(600 * est_cap * 0.5)} requests). Try the lighter profile: make profile P=lite")
    if save:
        cfg.write_load_config({"latency_idle": out})
    return out


def _fmt(v) -> str:
    return "   n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:6.2f}s"


def _step(load_cfg: dict, rps: float, seconds: float, seed: int) -> pd.DataFrame:
    segs = [{"at": 0, "rps_x": 1.0, "mix": load_cfg["default_mix"]}]
    res = runner._run_load_sync(load_cfg, segs, seconds, rps, seed, time.time() + 0.5)
    return pd.DataFrame(res.rows)


def _p95(ok: pd.DataFrame) -> float:
    return float(ok["ttft_s"].quantile(0.95)) if len(ok) >= 5 else math.inf


BASE_FRACTION = 0.75  # base_rps = BASE_FRACTION * capacity knee (see probe_capacity)


def probe_capacity(step_s: float = 45.0, factor: float = 1.25, max_steps: int = 16, degrade_x: float = 3.0,
                   max_error: float = 0.02, start_frac: float = 0.35, max_retreats: int = 3,
                   min_requests: int = 20, pause_s: float = 5.0, drain_s: float = 20.0, base_frac: float = BASE_FRACTION) -> dict:
    """Climb offered load against ONE llm replica until p95 TTFT degrades > degrade_x or errors exceed max_error.

    capacity = highest passing step (a LATENCY knee, not the throughput limit); base_rps = base_frac * capacity.
    The fraction must satisfy two things at once: the baseline stays below the knee, and the 3x burst of scenario s1
    exceeds what one replica can really serve (1 / mean service time). 0.5 satisfied the first and, on a laptop where
    the knee sits at ~55 % of the throughput limit, NOT the second (the "burst" then only reached ~80 % utilisation). Capacity depends on the class mix and profile (scenario s2 exploits exactly that).
    Never aborts just because the first rate was too high: it halves the start rate up to `max_retreats` times.
    """
    load_cfg = cfg.load_load()
    k8s.ensure_release_deployed()
    k8s.set_policy("static1")
    lat = measure_latency()  # doubles as the warm-up of the fresh pods
    start = max(0.02, round(start_frac * lat["est_capacity_rps"], 3))
    steps: list[dict] = []
    # Reference = TTFT p95 of requests sent ONE AT A TIME (genuinely unloaded). Never a step of the ramp itself:
    # an overloaded first step would otherwise become its own yardstick and always "pass".
    ref_p95 = max(0.5, lat["idle_ttft_p95_s"])
    last_good = None
    rps = start
    retreats = 0
    print(f"\n[capacity] idle estimate {lat['est_capacity_rps']:.2f} rps; first step at {start:.3f} rps, x{factor} per step "
          f"(about {max_steps * 1.5:.0f} min at most)")
    for i in range(max_steps):
        seconds = min(150.0, max(step_s, min_requests / rps))
        df = _step(load_cfg, rps, seconds, seed=7000 + i)
        ok = df[df["status"] == "ok"]
        err = 1.0 - len(ok) / max(1, len(df))
        p95 = _p95(ok)
        passed = err <= max_error and p95 <= degrade_x * ref_p95
        steps.append({"rps": round(rps, 3), "seconds": round(seconds), "n": int(len(df)), "error_ratio": round(err, 4),
                      "ttft_p95": None if not math.isfinite(p95) else round(p95, 3),
                      "total_mean": None if ok.empty else round(float(ok["total_s"].mean()), 3), "passed": bool(passed)})
        print(f"[capacity] rps={rps:.3f} n={len(df):3d} err={err:.3f} p95_ttft={_fmt(p95)} -> {'ok' if passed else 'DEGRADED'}")
        if not passed:
            if last_good is None and retreats < max_retreats:
                retreats += 1
                rps = round(rps / 2.0, 3)
                print(f"[capacity] even the first step degraded: retreating to {rps:.3f} rps ({retreats}/{max_retreats}); letting the queue drain")
                time.sleep(drain_s)
                continue
            break
        last_good = rps
        rps = round(rps * factor, 4)
        time.sleep(pause_s)
    if last_good is None:
        raise RuntimeError(
            f"the service degraded even at {rps:.3f} rps after {retreats} retreats (idle estimate was {lat['est_capacity_rps']:.2f} rps).\n"
            "The machine is not delivering the CPU the cluster expects. Check, in this order: (1) Windows power mode = Best performance and plugged in; "
            "(2) Docker Desktop's built-in Kubernetes is OFF; (3) `uptime` load average at idle < 1; (4) `.wslconfig` gives WSL >= 6 processors; "
            "(5) `kubectl -n slo-loop top pods` during a probe. Keep results/capacity/*.json and open an issue with them.")
    saturated = steps[-1]["passed"] is False
    last_good = round(last_good, 3)  # round ONCE: the saved capacity and base_rps must agree exactly
    base = max(0.02, round(base_frac * last_good, 3))
    ratio = last_good / lat["est_capacity_rps"]
    print(f"\n[capacity] capacity(1 replica) ~= {last_good:.3f} rps ({ratio:.0%} of the idle estimate); base_rps = {base}")
    if not saturated:
        print(f"[capacity] WARNING: never saturated in {max_steps} steps; capacity is a lower bound.")
    if ratio < 0.5:
        print("[capacity] NOTE: capacity is far below the idle estimate: CPU contention (other programs, power mode, thermal throttling).")
    expect = int(600 * base)
    print(f"[capacity] a 10-minute scenario at base_rps will hold ~{expect} requests." + (
        "  That is few: detectors will see sparse windows (see docs/DESIGN.md, 'low-traffic alerting')." if expect < 60 else ""))
    burst_util = 3 * base / lat["est_capacity_rps"]
    print(f"[capacity] a 3x burst offers {3 * base:.3f} rps = {burst_util:.0%} of the idle-latency throughput estimate" + (
        "" if burst_util >= 1.0 else "  (below 100 %: the burst will queue but not truly overload; consider `slo-lab set-base --frac 0.9`)"))
    record = {"capacity_1replica_rps": round(last_good, 3), "base_rps": base, "base_fraction": base_frac, "steps": steps, "idle": lat,
              "saturated": saturated, "retreats": retreats, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    outdir = cfg.RESULTS_DIR / "capacity"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"probe-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    cfg.write_load_config({"calibrated": True, "capacity_1replica_rps": round(last_good, 3), "base_rps": base,
                           "base_fraction": base_frac, "capacity_probe": steps})
    return record


def set_base_rps(frac: float) -> dict:
    """Recompute base_rps = frac * measured capacity WITHOUT re-running the 15-minute probe. Thresholds become stale."""
    load = cfg.load_load()
    cap = load.get("capacity_1replica_rps")
    if not load.get("calibrated") or not cap:
        raise RuntimeError("no measured capacity yet: run `make capacity` first")
    if not 0.2 <= frac <= 1.0:
        raise RuntimeError("--frac must be between 0.2 and 1.0 (a baseline above the measured knee is not a baseline)")
    base = max(0.02, round(frac * float(cap), 3))
    cfg.write_load_config({"base_rps": base, "base_fraction": frac})
    stale = cfg.invalidate_slo_calibration("slo-lab set-base", "the new offered load")
    est = (load.get("latency_idle") or {}).get("est_capacity_rps")
    print(f"[set-base] base_rps = {frac} x {cap} = {base} rps  (~{int(600 * base)} requests per 10-minute run)")
    if est:
        print(f"[set-base] a 3x burst offers {3 * base:.3f} rps = {3 * base / float(est):.0%} of the idle-latency throughput estimate ({est} rps)")
    if stale:
        print("[set-base] the SLO thresholds were measured at the old load and are now marked stale: run `make calibrate`")
    return {"base_rps": base, "fraction": frac}


def auto_calibration_seconds(base_rps: float, mix: dict, min_samples: int = 15, lo: float = 600.0, hi: float = 1500.0) -> float:
    """Long enough that the RAREST class collects >= `min_samples` requests at base_rps with ~95 % probability.

    Arrivals are Poisson, so an EXPECTED count of exactly `min_samples` would fall short half of the time (a 17-minute
    baseline thrown away). The expected count is therefore raised by 1.65 standard deviations. Bounded to [lo, hi] seconds.
    """
    rare = min(float(v) for v in mix.values() if float(v) > 0)
    need = min_samples + 1.65 * math.sqrt(min_samples)
    return float(min(hi, max(lo, math.ceil(need / (base_rps * rare)))))


def calibrate_slo(duration_s: float | None = None, headroom_x: float = 1.5, quantile: float = 0.99,
                  min_samples: int = 15) -> dict:
    """Derive per-class TTFT thresholds from a clean baseline: threshold = headroom_x * p99 (rounded up to 0.1 s)."""
    load_cfg = cfg.load_load()
    if not load_cfg.get("calibrated"):
        raise RuntimeError("run `make capacity` first (base_rps is not calibrated)")
    k8s.ensure_release_deployed()
    base_rps = float(load_cfg["base_rps"])
    if duration_s is None:
        duration_s = auto_calibration_seconds(base_rps, load_cfg["default_mix"], min_samples)
    print(f"[calibrate] {duration_s:.0f} s at {base_rps} rps (~{int(duration_s * base_rps)} requests); the rarest class needs >= {min_samples} samples")
    k8s.set_policy("static1")
    wait_until_quiet()
    runner.warmup(load_cfg, 45)
    segs = [{"at": 0, "rps_x": 1.0, "mix": load_cfg["default_mix"]}]
    res = runner._run_load_sync(load_cfg, segs, duration_s, base_rps, 31337, time.time() + 0.5)
    df = pd.DataFrame(res.rows)
    outdir = cfg.RESULTS_DIR / "calibration"  # the raw baseline is evidence: thresholds must be auditable
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    loadgen.write_rows(outdir / f"baseline-{stamp}.csv", res.rows)
    slo = cfg.load_slo()
    thr, prov = {}, {}
    for c in CLASSES:
        ok = df[(df["cls"] == c) & (df["status"] == "ok") & df["ttft_s"].notna()]
        hard_min = max(8, min_samples // 2)
        if len(ok) < hard_min:
            raise RuntimeError(f"only {len(ok)} successful '{c}' requests in calibration (need at least {hard_min}); "
                               "re-run with a longer `.venv/bin/slo-lab calibrate --duration 1800`")
        if len(ok) < min_samples:
            print(f"[calibrate] WARNING: only {len(ok)} '{c}' samples (target {min_samples}). The p99 of so few samples is close to their "
                  "maximum, so the threshold is conservative (looser). Re-run with a longer --duration for a tighter one.")
        q = float(ok["ttft_s"].quantile(quantile))
        thr[c] = max(0.5, math.ceil(headroom_x * q * 10) / 10)
        prov[c] = {"n_ok": int(len(ok)), f"p{int(quantile * 100)}": round(q, 3), "p50": round(float(ok["ttft_s"].median()), 3)}
    new = {"slo": {"calibrated": True, "target": slo["target"], "request_timeout_s": slo["request_timeout_s"],
                   "ttft_s": thr, "calibration": {"duration_s": duration_s, "headroom_x": headroom_x, "per_class": prov}}}
    (cfg.CONFIG_DIR / "slo.yaml").write_text("# Written by `make calibrate`.\n" + yaml.safe_dump(new, sort_keys=False), encoding="utf-8")
    k8s.helm_upgrade_full()
    idle = (load_cfg.get("latency_idle") or {}).get("per_class", {})
    print(f"\n[calibrate] class    n_ok   p50 TTFT   p99 TTFT   threshold ({headroom_x:g} x p99)   vs idle TTFT")
    for c in CLASSES:
        p = prov[c]
        rel = f"{thr[c] / idle[c]['ttft_mean_s']:5.1f}x" if c in idle and idle[c].get("ttft_mean_s") else "   n/a"
        print(f"[calibrate] {c:7s} {p['n_ok']:5d}   {p['p50']:7.2f}s   {p['p99']:7.2f}s   {thr[c]:9.1f}s                {rel}")
    if not (thr["short"] <= thr["medium"] <= thr["long"]):
        print("[calibrate] NOTE: thresholds are not ordered short <= medium <= long. At baseline load a request mostly WAITS behind other "
              "requests, so its TTFT reflects the queue more than its own prompt cost, and the p99 of this few samples is close to their "
              "maximum. That is a property of LLM serving at this load (a finding), not a bug; it also means the SLO is loose.")
    print(f"[calibrate] baseline saved to {outdir}/baseline-{stamp}.csv")
    print(f"[calibrate] thresholds (s): {thr}")
    return new
