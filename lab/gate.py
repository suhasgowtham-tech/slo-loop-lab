"""`make gate`: is this machine healthy RIGHT NOW? One idle `s0_baseline` run at a fixed single replica; PASS only when at
least `min_good` percent of its requests are good.

Why it exists (real incident, 10 Nov): the same lab scored 100 % good at night and 89 % good (median TTFT 3x higher) a few hours
later, with nothing changed in the lab. A long run started in that state measures the laptop, not the autoscaler. The gate makes the
check a command that FAILS (exit code 1), so a night can be chained behind it:  make gate && make phaseB REPS=3

Gate runs live in their own phase `G` (results/runs/G), numbered automatically, so they never collide with experiment runs and
`make analyze` never mixes them in. `slo-lab gate --history` prints every gate so far (a health time series of the machine).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

GATE_PHASE = "G"
GATE_SCENARIO = "s0_baseline"
GATE_POLICY = "static1"
DEFAULT_MIN_GOOD_PCT = 97.0


def next_rep(runs_dir: Path) -> int:
    """1 + the highest rep number already used by a gate run (finished or not)."""
    best = 0
    d = runs_dir / GATE_PHASE
    if d.exists():
        for p in d.iterdir():
            m = re.search(rf"^{GATE_SCENARIO}__{GATE_POLICY}__r(\d+)$", p.name)
            if m:
                best = max(best, int(m.group(1)))
    return best + 1


def running_containers() -> list[str] | None:
    """Names of running Docker containers, or None when Docker cannot be reached."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def stray_containers(running: list[str] | None, node: str) -> list[str]:
    return sorted(n for n in (running or []) if n != node)


def evaluate(run: Path, min_good_pct: float = DEFAULT_MIN_GOOD_PCT) -> dict:
    """Verdict for one finished gate run: {passed, good_pct, p50, p95, n, valid, reasons}."""
    from .analysis import slo as slo_mod

    meta = json.loads((run / "meta.json").read_text())
    df = slo_mod.annotate(slo_mod.load_requests(run / "requests.csv"), meta["slo"]["ttft_s"])
    allrow = slo_mod.class_stats(df)["all"]
    n = int(allrow["n"])
    good = allrow["good_ratio"]
    reasons: list[str] = []
    if not meta.get("valid", False):
        reasons.append("run is flagged invalid: " + "; ".join(meta.get("invalid_reasons") or ["no reason recorded"]))
    if n == 0 or good is None:
        reasons.append("the run recorded no requests")
    good_pct = None if good is None else 100.0 * float(good)
    if good_pct is not None and good_pct < min_good_pct:
        reasons.append(f"{good_pct:.1f}% good is below the {min_good_pct:.0f}% gate")
    short = slo_mod.class_stats(df).get("short") or {}
    return {
        "passed": not reasons, "good_pct": good_pct, "p50": allrow.get("ttft_p50"), "p95": allrow.get("ttft_p95"),
        "short_p50": short.get("ttft_p50"),
        "n": n, "valid": bool(meta.get("valid", False)), "reasons": reasons, "start_epoch": meta.get("start_epoch"),
    }


def format_verdict(v: dict, run_name: str, min_good_pct: float) -> str:
    f = lambda x: "n/a" if x is None else f"{x:.2f} s"  # noqa: E731
    good = "n/a" if v["good_pct"] is None else f"{v['good_pct']:.1f}%"
    head = "GATE PASS" if v["passed"] else "GATE FAIL"
    line = (f"{head}: {v['n']} requests, {good} good (need at least {min_good_pct:.0f}%), TTFT median {f(v['p50'])}, p95 {f(v['p95'])}, "
            f"short-prompt median {f(v.get('short_p50'))}   [{run_name}]")
    if v["passed"]:
        return line + "\nThe machine is healthy right now. A long run may start (and run `make gate` again when it ends)."
    return (line + "\n  why: " + "; ".join(v["reasons"]) +
            "\nThe machine is NOT healthy right now. Do NOT start a long run: it would measure the laptop, not the autoscaler.\n"
            "Fix, in order: plug in the charger; Windows power mode Best performance; CLOSE Chrome, ChatGPT, WhatsApp, Teams and other apps and "
            "do not touch the laptop while a gate or a run is measuring (using it is the prime suspect); make sure nothing else runs in Docker "
            "(`make diag` lists containers); wait 10 minutes untouched; then `make gate` again.\n"
            "(The overall median depends on which traffic sample this gate drew; the short-prompt median is comparable between gates: about 0.6 s when healthy.)")


def history(runs_dir: Path) -> list[dict]:
    """[{name, start_epoch, n, good_pct, p50, p95}] of every finished gate run, oldest first."""
    rows = []
    d = runs_dir / GATE_PHASE
    if not d.exists():
        return rows
    for p in sorted(d.iterdir()):
        if not (p / "DONE").exists() or not (p / "meta.json").exists():
            continue
        try:
            v = evaluate(p, 0.0)
        except (OSError, KeyError, ValueError):
            continue
        rows.append({"name": p.name, "start_epoch": v["start_epoch"], "n": v["n"], "good_pct": v["good_pct"], "p50": v["p50"], "p95": v["p95"],
                     "short_p50": v["short_p50"]})
    rows.sort(key=lambda r: (r["start_epoch"] or 0.0, r["name"]))
    return rows


def format_history(rows: list[dict], min_good_pct: float) -> str:
    if not rows:
        return "no gate runs yet: run `make gate`"
    out = ["gate history (times are UTC; India = UTC + 5:30):",
           "  run                          started (UTC)     n    good%   p50     p95   short p50  verdict"]
    for r in rows:
        when = "?" if not r["start_epoch"] else time.strftime("%d-%b %H:%M", time.gmtime(float(r["start_epoch"])))
        good = "n/a" if r["good_pct"] is None else f"{r['good_pct']:5.1f}"
        verdict = "-" if r["good_pct"] is None else ("pass" if r["good_pct"] >= min_good_pct else "FAIL")
        p50 = "  n/a" if r["p50"] is None else f"{r['p50']:5.2f}s"
        p95 = "  n/a" if r["p95"] is None else f"{r['p95']:5.2f}s"
        sp = "    n/a" if r.get("short_p50") is None else f"{r['short_p50']:6.2f}s"
        out.append(f"  {r['name']:28s} {when:15s} {r['n']:4d}  {good:>6s}  {p50}  {p95}  {sp}   {verdict}")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------- per-run health (`make health`)
# A run's own steady-load stretch at the start (after the 20 s ramp-in, before the load or the fault changes) is a free health probe:
# every policy sees the same traffic there, so a slow or failing stretch means the HOST was slow, not that the autoscaler was bad.
HOSTLOG_NAME = "hostlog.csv"
PRE_FROM_S = 20.0
PRE_MARGIN_S = 10.0
MIN_PRE_N = 12            # fewer requests than this: no verdict on good%
BAD_PRE_PCT = 5.0          # steady-stretch bad% at or above this: flag BAD
SLOW_FACTOR = 2.0          # short-prompt median at or above this x the fastest in the table: flag SLOW
IST_OFFSET_S = 19800


def steady_end(scenario: str, events: dict) -> float | None:
    """Seconds after the start where the first constant-load, fault-free stretch ends (None = whole run)."""
    ends: list[float] = []
    starts = [float(i["start"]) for i in events.get("incidents", [])]
    if starts:
        ends.append(min(starts) - PRE_MARGIN_S)
    try:
        from . import config as cfg
        load = cfg.load_scenario(scenario).get("load", [])
        if len(load) > 1:
            ends.append(float(load[1]["at"]) - 5.0)
    except Exception:  # noqa: BLE001 - unknown scenario: fall back to the incident start
        pass
    return min(ends) if ends else None


def run_health(run: Path) -> dict:
    from .analysis import slo as slo_mod

    meta = json.loads((run / "meta.json").read_text())
    ev_path = run / "events.json"
    events = json.loads(ev_path.read_text()) if ev_path.exists() else {}
    df = slo_mod.annotate(slo_mod.load_requests(run / "requests.csv"), meta["slo"]["ttft_s"])
    end = steady_end(str(meta.get("scenario", "")), events)
    pre = df[(df["t_send"] >= PRE_FROM_S) & (df["t_send"] < (float("inf") if end is None else end))]
    ok = pre[(pre["status"] == "ok") & pre["ttft_s"].notna()]
    short = ok[ok["cls"] == "short"]
    return {
        "name": run.name, "phase": run.parent.name, "scenario": meta.get("scenario"), "policy": meta.get("policy"), "rep": meta.get("rep"),
        "valid": bool(meta.get("valid", False)), "start_epoch": meta.get("start_epoch"), "end_epoch": meta.get("end_epoch"),
        "n": int(len(df)), "good_pct": None if not len(df) else 100.0 * float(df["good"].mean()),
        "pre_n": int(len(pre)), "pre_good_pct": None if not len(pre) else 100.0 * float(pre["good"].mean()),
        "short_n": int(len(short)), "short_p50": float(short["ttft_s"].quantile(0.5)) if len(short) else None,
        "attempts": int(meta.get("attempts", 1)),
    }


def load_hostlog(path: Path):
    """The host log as a DataFrame, or None when there is none (or it is unreadable)."""
    import pandas as pd

    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        return df if len(df) and "epoch" in df.columns else None
    except Exception:  # noqa: BLE001
        return None


def host_window(hostlog, t0, t1) -> dict:
    """Mean CPU busy %, mean steal %, lowest available memory (MB) of the host-log samples inside [t0, t1]."""
    if hostlog is None or not t0 or not t1:
        return {"busy": None, "steal": None, "mem_min": None}
    part = hostlog[(hostlog["epoch"] >= float(t0)) & (hostlog["epoch"] <= float(t1))]
    col = lambda c: None if c not in part or part[c].dropna().empty else float(part[c].astype(float).mean())  # noqa: E731
    mem = None if "mem_avail_mb" not in part or part["mem_avail_mb"].dropna().empty else float(part["mem_avail_mb"].astype(float).min())
    return {"busy": col("cpu_busy_pct"), "steal": col("cpu_steal_pct"), "mem_min": mem}


def flag_runs(rows: list[dict]) -> None:
    """Adds row['flags'] (list of INVALID / BAD / SLOW) in place."""
    shorts = [r["short_p50"] for r in rows if r["short_p50"] is not None and r["short_n"] >= 5]
    best = min(shorts) if shorts else None
    for r in rows:
        f = []
        if not r["valid"]:
            f.append("INVALID")
        if r["pre_n"] >= MIN_PRE_N and r["pre_good_pct"] is not None and 100.0 - r["pre_good_pct"] >= BAD_PRE_PCT:
            f.append("BAD")
        if best and r["short_p50"] is not None and r["short_n"] >= 5 and r["short_p50"] >= SLOW_FACTOR * best:
            f.append("SLOW")
        # A flag a person has accepted (see accept_flagged) is shown as "accepted:..." and no longer counts as flagged. INVALID never can be.
        acc = r.get("accepted") or set()
        r["flags"] = [x for x in f if x == "INVALID" or x not in acc]
        r["accepted_flags"] = [x for x in f if x != "INVALID" and x in acc]


ACCEPT_NAME = "accepted-flags.csv"   # results/accepted-flags.csv: epoch,phase,run,flags,attempts,note


def load_accepted(path: Path) -> dict[tuple[str, str], set[str]]:
    """(phase, run name) -> the flags (BAD / SLOW) a person decided to keep. Missing or unreadable file = nothing accepted.

    The key MUST include the phase: the same scenario + policy + rep exists in Phase A and in Phase B (s2_long_shift__static1__r2 does),
    and v2.5.9 keyed by name only, so keeping the Phase B run silently kept the Phase A run too (found on 18 Nov: `make accept-flagged
    PHASE=A` said "kept 2" while `make status` counted 3)."""
    import csv

    out: dict[tuple[str, str], set[str]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("run"):
                    out.setdefault((row.get("phase") or "", row["run"]), set()).update(x for x in (row.get("flags") or "").split("|") if x in ("BAD", "SLOW"))
    except OSError:
        pass
    return out


def accept_flagged(path: Path, rows: list[dict], note: str, now: float | None = None) -> tuple[list[dict], list[dict]]:
    """Record every currently flagged BAD/SLOW run as accepted. Returns (accepted rows, refused rows); INVALID runs are refused."""
    import csv

    todo = [r for r in rows if r.get("flags")]
    refused = [r for r in todo if "INVALID" in r["flags"]]
    take = [r for r in todo if "INVALID" not in r["flags"]]
    if take:
        new = not path.exists() or path.stat().st_size == 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["epoch", "phase", "run", "flags", "attempts", "note"])
            for r in take:
                w.writerow([int(time.time() if now is None else now), r["phase"], r["name"], "|".join(r["flags"]), r.get("attempts", 1), note.replace(",", ";")])
    return take, refused


def format_health(rows: list[dict], hostlog_present: bool) -> str:
    if not rows:
        return "no finished runs found"
    flag_runs(rows)
    fnum = lambda v, fmt: "  n/a" if v is None else fmt.format(v)  # noqa: E731
    head = ("  run                            start IST     ok   n  good%  steady n  steady good%  short p50"
            + ("  host busy%  steal%  min free MB" if hostlog_present else "") + "  flags")
    out = ["per-run health (steady stretch = the first 20 s .. before the load or fault changes; same traffic for every policy)", head]
    for r in sorted(rows, key=lambda r: (r["start_epoch"] or 0.0, r["name"])):
        when = "?" if not r["start_epoch"] else time.strftime("%d-%b %H:%M", time.gmtime(float(r["start_epoch"]) + IST_OFFSET_S))
        line = (f"  {r['name']:30s} {when:12s}  {'yes' if r['valid'] else 'NO':3s} {r['n']:4d} {fnum(r['good_pct'], '{:5.1f}'):>6s}  "
                f"{r['pre_n']:8d}  {fnum(r['pre_good_pct'], '{:12.1f}'):>12s}  {fnum(r['short_p50'], '{:7.2f}s'):>9s}")
        if hostlog_present:
            line += f"  {fnum(r.get('busy'), '{:9.0f}'):>10s}  {fnum(r.get('steal'), '{:6.1f}'):>6s}  {fnum(r.get('mem_min'), '{:11.0f}'):>11s}"
        line += "  " + (",".join(r["flags"]) or (("accepted:" + ",".join(r["accepted_flags"])) if r.get("accepted_flags") else "-"))
        out.append(line)
    shorts = [r["short_p50"] for r in rows if r["short_p50"] is not None and r["short_n"] >= 5]
    if shorts:
        out.append(f"\nfastest short-prompt median in this table: {min(shorts):.2f}s. SLOW = {SLOW_FACTOR:g}x that or more; BAD = {BAD_PRE_PCT:g}% or more of "
                   f"the steady-stretch requests bad (needs {MIN_PRE_N}+ requests). A run flagged BAD or SLOW was probably measured on a slow host: do not trust it, re-run it with --force.")
    return "\n".join(out)


def health_rows(runs_dir: Path, phase: str | None) -> list[dict]:
    rows = []
    phases = [runs_dir / phase] if phase else sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.exists() else []
    hostlog = load_hostlog(runs_dir.parent / HOSTLOG_NAME)
    accepted = load_accepted(runs_dir.parent / ACCEPT_NAME)
    for pdir in phases:
        if not pdir.exists():
            continue
        for p in sorted(pdir.iterdir()):
            if not ((p / "DONE").exists() and (p / "meta.json").exists() and (p / "requests.csv").exists()):
                continue
            try:
                r = run_health(p)
            except (OSError, KeyError, ValueError):
                continue
            r.update(host_window(hostlog, r["start_epoch"], r["end_epoch"]))
            r["accepted"] = accepted.get((r["phase"], r["name"]), set())
            rows.append(r)
    return rows


GATE_MAX_AGE_S = 5400.0   # a passing gate vouches for the machine for 90 minutes


def recent_gate_ok(runs_dir: Path, min_good_pct: float = DEFAULT_MIN_GOOD_PCT, max_age_s: float = GATE_MAX_AGE_S, now: float | None = None):
    """(ok, message): did the latest gate pass, and recently enough? Used to refuse re-runs on a machine nobody has vouched for."""
    rows = history(runs_dir)
    if not rows:
        return False, "no gate run yet: run `make gate` first"
    last = rows[-1]
    if last["good_pct"] is None or last["good_pct"] < min_good_pct:
        return False, f"the latest gate ({last['name']}) FAILED ({'n/a' if last['good_pct'] is None else format(last['good_pct'], '.1f')}% good): run `make gate` until it passes"
    age = (time.time() if now is None else now) - float(last["start_epoch"] or 0.0)
    if age > max_age_s:
        return False, f"the latest passing gate is {age / 60:.0f} minutes old (limit {max_age_s / 60:.0f}): run `make gate` again"
    return True, f"latest gate {last['name']} passed {age / 60:.0f} minutes ago"
