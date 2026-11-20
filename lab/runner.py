"""Experiment runner: one (scenario, policy, rep) -> one self-describing run directory."""
from __future__ import annotations

import asyncio
import csv
import json
import os
import platform
import subprocess
import threading
import time
import zlib
from pathlib import Path

import httpx

from . import config as cfg
from . import k8s, loadgen, prom

MAX_LAG_S = 0.25  # client scheduling lag above this invalidates a run (the load generator was starved)


def run_dir(phase: str, scenario: str, policy: str, rep: int) -> Path:
    return cfg.RUNS_DIR / phase / f"{scenario}__{policy}__r{rep}"


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(cfg.ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _mem_total_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def capabilities() -> dict:
    """Result of `make chaos-preflight` (config/capabilities.yaml); unknown => assume unsupported."""
    path = cfg.CONFIG_DIR / "capabilities.yaml"
    return cfg.load_yaml(path) if path.exists() else {}


class ReplicaPoller(threading.Thread):
    def __init__(self, path: Path, interval_s: float = 5.0):
        super().__init__(daemon=True)
        self.path, self.interval_s, self._halt = path, interval_s, threading.Event()

    def run(self) -> None:
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["epoch", "spec", "ready"])
            while not self._halt.is_set():
                try:
                    spec, ready = k8s.deployment_replicas("llm")
                    w.writerow([f"{time.time():.3f}", spec, ready])
                    f.flush()
                except Exception:  # noqa: BLE001 - polling must never kill a run
                    pass
                self._halt.wait(self.interval_s)

    def stop(self) -> None:
        self._halt.set()


class FaultScheduler(threading.Thread):
    """Applies/reverts faults at absolute epochs; records what actually happened."""

    def __init__(self, faults: list[dict], start_epoch: float, slo: dict):
        super().__init__(daemon=True)
        self.faults, self.start_epoch, self.slo = faults, start_epoch, slo
        self.events: list[dict] = []
        self._halt = threading.Event()
        self.errors: list[str] = []

    def _sleep_until(self, epoch: float) -> bool:
        while not self._halt.is_set():
            left = epoch - time.time()
            if left <= 0:
                return True
            self._halt.wait(min(left, 0.5))
        return False

    def _fault_body(self, f: dict) -> dict:
        p = dict(f.get("params", {}))
        if "latency_x_short_thr" in p:
            p["latency_ms"] = float(p.pop("latency_x_short_thr")) * float(self.slo["ttft_s"]["short"]) * 1000.0
        return p

    def _apply(self, f: dict) -> None:
        if f["kind"] == "chaos":
            k8s.apply_manifest(str(cfg.CHAOS_DIR / f["manifest"]))
        elif f["kind"] == "retriever_fault":
            r = httpx.post(f"{cfg.GATEWAY_URL}/admin/retriever-fault", json=self._fault_body(f), timeout=10)
            r.raise_for_status()
        else:
            raise ValueError(f"unknown fault kind {f['kind']!r}")

    def _revert(self, f: dict) -> None:
        if f["kind"] == "chaos":
            k8s.delete_manifest(str(cfg.CHAOS_DIR / f["manifest"]))
        elif f["kind"] == "retriever_fault":
            httpx.delete(f"{cfg.GATEWAY_URL}/admin/retriever-fault", timeout=10)

    def run(self) -> None:
        plan = []
        for f in self.faults:
            plan.append((self.start_epoch + float(f["at"]), "apply", f))
            plan.append((self.start_epoch + float(f["until"]), "revert", f))
        for epoch, action, f in sorted(plan, key=lambda x: x[0]):
            if not self._sleep_until(epoch):
                return
            t = time.time()
            try:
                (self._apply if action == "apply" else self._revert)(f)
                self.events.append({"action": action, "kind": f["kind"], "manifest": f.get("manifest"), "t_rel": t - self.start_epoch, "ok": True})
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{action} {f['kind']}: {e}")
                self.events.append({"action": action, "kind": f["kind"], "t_rel": t - self.start_epoch, "ok": False, "error": str(e)})

    def stop(self) -> None:
        self._halt.set()


def cleanup_faults() -> None:
    for p in cfg.CHAOS_DIR.glob("*.yaml"):
        k8s.delete_manifest(str(p))
    try:
        httpx.delete(f"{cfg.GATEWAY_URL}/admin/retriever-fault", timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _run_load_sync(load_cfg: dict, segments: list[dict], duration_s: float, base_rps: float, seed: int, start_epoch: float):
    arrivals = loadgen.build_arrivals(segments, duration_s, base_rps, seed)
    bodies = loadgen.prepare_bodies(arrivals, load_cfg["classes"], seed)
    client = load_cfg.get("client", {})
    return asyncio.run(
        loadgen.run_load(
            cfg.GATEWAY_URL, arrivals, bodies,
            request_timeout_s=float(client.get("request_timeout_s", 90)),
            max_outstanding=int(client.get("max_outstanding", 400)),
            start_epoch=start_epoch,
        )
    )


def warmup(load_cfg: dict, seconds: float = 45.0) -> None:
    base = float(load_cfg["base_rps"])
    segs = [{"at": 0, "rps_x": 0.5, "mix": load_cfg["default_mix"]}]
    _run_load_sync(load_cfg, segs, seconds, base, seed=424242, start_epoch=time.time() + 0.5)


def check_scenario_runnable(scenario: dict, slo: dict) -> None:
    """Refuse (in milliseconds, before any 12-minute run) a scenario whose fault cannot behave as designed with the
    deployment that is running. Today: a retriever delay at or above the gateway's retriever timeout would not make
    requests slow, it would make every one of them FAIL (the scenario would silently become an outage)."""
    for f in scenario.get("faults", []):
        p = f.get("params", {})
        if f.get("kind") == "retriever_fault" and "latency_x_short_thr" in p:
            latency_s = float(p["latency_x_short_thr"]) * float(slo["ttft_s"]["short"])
            timeout_s = k8s.retrieve_timeout_s()
            if latency_s >= 0.9 * timeout_s:
                raise k8s.ScenarioNotRunnable(
                    f"scenario {scenario.get('name', '?')} injects a retriever delay of {latency_s:.1f} s "
                    f"({p['latency_x_short_thr']} x the short-class threshold {slo['ttft_s']['short']} s), but the gateway gives up on the "
                    f"retriever after {timeout_s:.0f} s, so every request in the incident would FAIL instead of being slow. "
                    "Run `make deploy` (the chart now sets RETRIEVE_TIMEOUT_S=30) and then `make verify`."
                )


def run_one(
    phase: str, scenario_name: str, policy: str, rep: int, *,
    force: bool = False, duration_override: float | None = None, prepare: bool = True,
) -> Path:
    scenario = cfg.load_scenario(scenario_name)
    slo = cfg.load_slo()
    load_cfg = cfg.load_load()
    out = run_dir(phase, scenario_name, policy, rep)
    if (out / "DONE").exists() and not force:
        print(f"[skip] {out.name} already done")
        return out
    if "netem" in scenario.get("requires", []) and not capabilities().get("netem", False):
        print(f"[skip] {scenario_name}: netem not supported here (run `make chaos-preflight`)")
        return out
    if prepare:
        check_scenario_runnable(scenario, slo)
    out.mkdir(parents=True, exist_ok=True)
    attempts = 1   # how many times this exact run was measured: a re-run overwrites the data, so this is the only trace (v2.5.9)
    try:
        if (out / "meta.json").exists():
            attempts = int(json.loads((out / "meta.json").read_text()).get("attempts", 1)) + 1
    except (OSError, ValueError, TypeError):
        attempts = 1
    (out / "DONE").unlink(missing_ok=True)

    duration = float(duration_override or scenario["duration_s"])
    seed = _stable_seed(scenario_name, rep)
    reasons: list[str] = []

    if prepare:
        print(f"[run] {out.name}: preparing policy={policy}")
        k8s.set_policy(policy)
        warm = float(scenario.get("warmup_s", 45))
        print(f"[run] {out.name}: warm-up {warm:.0f}s")
        warmup(load_cfg, warm)
        time.sleep(5)

    spec0, ready0 = k8s.deployment_replicas("llm") if prepare else (0, 0)
    expected = int(policy.removeprefix("static")) if policy.startswith("static") else 1
    if prepare and ready0 != expected:
        reasons.append(f"replicas at t0 were {ready0}, expected {expected}")

    start_epoch = time.time() + 3.0
    poller = ReplicaPoller(out / "replicas.csv")
    scheduler = FaultScheduler(scenario["faults"], start_epoch, slo)
    if prepare:
        poller.start()
    scheduler.start()
    print(f"[run] {out.name}: measuring {duration:.0f}s")
    try:
        result = _run_load_sync(load_cfg, scenario["load"], duration, float(load_cfg["base_rps"]), seed, start_epoch)
    finally:
        time.sleep(1)
        scheduler.stop()
        poller.stop()
        if prepare:
            cleanup_faults()
    end_epoch = time.time()

    if result.max_lag_s > MAX_LAG_S:
        reasons.append(f"load generator lag {result.max_lag_s:.3f}s > {MAX_LAG_S}s (client starved; close other apps)")
    if result.n_dropped:
        reasons.append(f"{result.n_dropped} requests dropped client-side (max_outstanding hit)")
    if result.n_scheduled and len(result.rows) != result.n_scheduled:
        reasons.append(f"{result.n_scheduled - len(result.rows)} of {result.n_scheduled} scheduled requests are missing from the result")
    if scheduler.errors:
        reasons.extend(f"fault error: {e}" for e in scheduler.errors)
    if not load_cfg.get("calibrated") or not slo.get("calibrated"):
        reasons.append("capacity/SLO not calibrated (run `make capacity` and `make calibrate`)")

    loadgen.write_rows(out / "requests.csv", result.rows)
    incidents = [dict(i) for i in scenario["incidents"]]
    (out / "events.json").write_text(json.dumps({"incidents": incidents, "fault_events": scheduler.events}, indent=2), encoding="utf-8")
    prom_ok = prom.snapshot(start_epoch, end_epoch, out / "prom.json") if prepare else False

    meta = {
        "phase": phase, "scenario": scenario_name, "policy": policy, "rep": rep, "seed": seed,
        "duration_s": duration, "start_epoch": start_epoch, "end_epoch": end_epoch, "attempts": attempts,
        "valid": not [r for r in reasons if not r.startswith("capacity/SLO not calibrated")],
        "invalid_reasons": reasons, "max_lag_s": result.max_lag_s, "n_requests": len(result.rows), "n_scheduled": result.n_scheduled,
        "prometheus_snapshot": prom_ok,
        "slo": slo, "load": {k: v for k, v in load_cfg.items() if k != "client"},
        "git_sha": _git_sha(), "host": {"python": platform.python_version(), "cpu_count": os.cpu_count(), "mem_total_mb": _mem_total_mb()},
        "tools": _tool_versions() if prepare else {},
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    (out / "DONE").write_text("ok\n", encoding="utf-8")
    print(f"[done] {out.name}: {len(result.rows)} requests, valid={meta['valid']}" + (f" ({'; '.join(reasons)})" if reasons else ""))
    return out


def _stable_seed(scenario: str, rep: int) -> int:
    return zlib.crc32(f"{scenario}:{rep}".encode()) & 0x7FFFFFFF


def _tool_versions() -> dict:
    out = {}
    for name, cmd in {
        "kubectl": ["kubectl", "version", "--client", "-o", "json"],
        "helm": ["helm", "version", "--short"],
        "kind": ["kind", "version"],
    }.items():
        try:
            out[name] = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()[:400]
        except Exception:  # noqa: BLE001
            out[name] = None
    try:
        out["helm_values"] = json.loads(k8s.helm("get", "values", cfg.RELEASE, "-n", cfg.NAMESPACE, "-o", "json", "--all"))
    except Exception:  # noqa: BLE001
        out["helm_values"] = None
    lock = cfg.ROOT / "platform" / "versions.lock"
    out["platform_lock"] = lock.read_text() if lock.exists() else None
    return out
