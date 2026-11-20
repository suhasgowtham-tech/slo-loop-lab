"""`slo-lab status` (make status): where does the lab stand, and what is the ONE next command?

Read-only. It looks at Docker, the kind node, the three deployments, the host logger, the finished run folders and the gate history,
compares them with what the experiment plan needs (TARGET_REPS) and prints the next step. It exists because a Windows/WSL restart
can cut a long run at any moment and the person at the keyboard then has to work out what is still missing; this answers that in
one screen. Nothing here starts, stops or changes anything.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfg
from . import gate, k8s

IST = timezone(timedelta(hours=5, minutes=30))
# What the plan needs. Phase B: every scenario x policy, 3 reps. Phase A: 6 reps, except the slow-burn scenario (H2), which gets 13.
TARGET_REPS = {"A": {"default": 6, "s7_slow_burn": 13}, "B": {"default": 3}}
PHASE_TITLE = {"A": "Phase A (detection, H1/H2/H4)", "B": "Phase B (autoscaling, H3)"}
MINUTES_PER_RUN = 11.0   # measured on the hands-off runs: 10.7 minutes per run, start to start
HOSTLOG_ALIVE_S = 120.0
RECENT_RESTART_S = 3600.0
PHASE_ORDER = ("B", "A")
S7 = "s7_slow_burn"


def run_name(sc: str, pol: str, rep: int) -> str:
    return f"{sc}__{pol}__r{rep}"


def target_jobs(phase: str, matrix: dict | None = None, netem_ok: bool | None = None) -> list[tuple[str, str, int]]:
    """Every (scenario, policy, rep) the plan needs for this phase. Scenarios that need netem are left out when this machine cannot do it."""
    m = (matrix if matrix is not None else cfg.load_matrix())[phase]
    reps = TARGET_REPS[phase]
    jobs: list[tuple[str, str, int]] = []
    for sc in m["scenarios"]:
        if netem_ok is not None and not netem_ok:
            try:
                if "netem" in cfg.load_scenario(sc).get("requires", []):
                    continue
            except Exception:  # noqa: BLE001 - a scenario file we cannot read must not break the status screen
                pass
        n = reps.get(sc, reps["default"])
        jobs += [(sc, pol, r) for pol in m["policies"] for r in range(1, n + 1)]
    return jobs


def progress(runs_dir: Path, phase: str, matrix: dict | None = None, netem_ok: bool | None = None) -> dict:
    jobs = target_jobs(phase, matrix, netem_ok)
    pdir = runs_dir / phase
    done, missing = 0, []
    for sc, pol, rep in jobs:
        if (pdir / run_name(sc, pol, rep) / "DONE").exists():
            done += 1
        else:
            missing.append((sc, pol, rep))
    interrupted: list[str] = []
    last: tuple[str, float] | None = None
    if pdir.exists():
        for p in sorted(pdir.iterdir()):
            if not p.is_dir():
                continue
            marker = p / "DONE"
            if marker.exists():
                t = marker.stat().st_mtime
                if last is None or t > last[1]:
                    last = (p.name, t)
            else:
                interrupted.append(p.name)
    return {"phase": phase, "target": len(jobs), "done": done, "missing": missing, "interrupted": interrupted, "last_done": last}


# --------------------------------------------------------------------------------------------------------------------- probes
def _uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _hostlog_tail(path: Path) -> dict | None:
    """Newest line of results/hostlog.csv: {epoch, busy, steal, mem_mb, top}."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 4096))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        parts = ln.split(",")
        if len(parts) >= 5 and parts[0].strip().isdigit():
            def num(x: str) -> float | None:
                try:
                    return float(x)
                except ValueError:
                    return None
            return {"epoch": float(parts[0]), "busy": num(parts[2]), "steal": num(parts[3]), "mem_mb": num(parts[4]),
                    "top": parts[7].strip() if len(parts) > 7 else ""}
    return None


_BUSY_RE = re.compile(r"slo-lab\s+(matrix|gate|run|recover|capacity|calibrate|latency)\b|slo-lab\s+health\b.*--rerun")


def _lab_processes() -> list[str]:
    """Command lines of lab commands that are running right now (a second one started on top would corrupt both)."""
    try:
        out = subprocess.run(["pgrep", "-af", "slo-lab"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    me = str(os.getpid())
    found: list[str] = []
    for ln in out.stdout.splitlines():
        pid, _, cmd = ln.strip().partition(" ")
        if pid == me or "--history" in cmd:
            continue
        m = _BUSY_RE.search(cmd)
        if m:
            short = cmd[m.start():].strip()
            if short not in found:
                found.append(short)
    return found


def _failures_tail(path: Path, n: int = 3) -> list[str]:
    try:
        return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-n:]
    except OSError:
        return []


def gather(runs_dir: Path | None = None, now: float | None = None) -> dict:
    runs_dir = runs_dir or cfg.RUNS_DIR
    now = time.time() if now is None else now
    s: dict = {"now": now, "runs_dir": runs_dir}
    up = _uptime_s()
    s["wsl_up_s"] = up
    s["processes"] = _lab_processes()
    node = f"{cfg.KUBE_CONTEXT.removeprefix('kind-')}-control-plane"
    running = gate.running_containers()
    s["node"] = node
    s["docker"] = running is not None
    s["node_running"] = bool(running) and node in (running or [])
    s["stray"] = gate.stray_containers(running, node) if running is not None else []
    s["deployments"] = {}
    s["autoscalers"] = []
    if s["node_running"]:
        for d in ("llm", "gateway", "retriever"):
            try:
                s["deployments"][d] = k8s.deployment_replicas(d)
            except Exception:  # noqa: BLE001
                s["deployments"][d] = None
        try:
            s["autoscalers"] = k8s.autoscalers_present()
        except Exception:  # noqa: BLE001
            s["autoscalers"] = []
    s["verified"] = (cfg.ROOT / ".lab-verified").exists()
    s["hostlog"] = _hostlog_tail(runs_dir.parent / gate.HOSTLOG_NAME)
    try:
        hist = gate.history(runs_dir)
    except Exception:  # noqa: BLE001
        hist = []
    s["last_gate"] = hist[-1] if hist else None
    s["gate_ok"] = gate.recent_gate_ok(runs_dir, now=now)
    netem_ok = None
    try:
        from . import runner
        netem_ok = bool(runner.capabilities().get("netem", False))
    except Exception:  # noqa: BLE001
        pass
    s["phases"] = {}
    s["flagged"] = {}
    s["persistent"] = {}   # flagged runs that were measured at least twice: the flag is reproducible, re-running again will not help
    s["accepted"] = {}     # flags a person kept with `make accept-flagged`
    for p in PHASE_ORDER:
        try:
            s["phases"][p] = progress(runs_dir, p, netem_ok=netem_ok)
        except Exception:  # noqa: BLE001
            s["phases"][p] = {"phase": p, "target": 0, "done": 0, "missing": [], "interrupted": [], "last_done": None}
        try:
            rows = gate.health_rows(runs_dir, p)
            gate.flag_runs(rows)    # health_rows() does NOT set the flags (format_health() does); v2.5.7 forgot this and always reported 0 flagged
            s["flagged"][p] = [f"{r['name']} [{','.join(r['flags'])}]" for r in rows if r["flags"]]
            s["persistent"][p] = [f"{r['name']} [{','.join(r['flags'])}]" for r in rows if r["flags"] and r.get("attempts", 1) >= 2 and "INVALID" not in r["flags"]]
            s["accepted"][p] = sum(1 for r in rows if r.get("accepted_flags"))
        except Exception:  # noqa: BLE001
            s["flagged"][p] = None  # unknown: never report "0 flagged" when the check itself failed
    s["failures"] = _failures_tail(runs_dir.parent / "matrix-failures.log")
    return s


# --------------------------------------------------------------------------------------------------------------------- decision
def _ago(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 90:
        return f"{sec:.0f} s"
    if sec < 5400:
        return f"{sec / 60:.0f} min"
    if sec < 2 * 86400:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 86400:.1f} days"


def _ist(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, IST).strftime("%a %d-%b %H:%M") + " IST"


def phase_commands(phase: str, missing: list[tuple[str, str, int]]) -> list[str]:
    if phase == "B":
        return ["make phaseB REPS=3"]
    cmds = []
    if any(sc != S7 for sc, _, _ in missing):
        cmds.append("make phaseA REPS=6")
    if any(sc == S7 for sc, _, _ in missing):
        cmds.append(f".venv/bin/slo-lab matrix --phase A --reps {TARGET_REPS['A'][S7]} --only {S7}")
    return cmds


def decide(s: dict) -> dict:
    """-> {headline, steps: [("say"|"cmd", text)], after: [str], warnings: [str]} (pure function of the gathered state)."""
    warnings: list[str] = []
    now = s["now"]
    up = s.get("wsl_up_s")
    if up is not None and up < RECENT_RESTART_S:
        warnings.append(f"Ubuntu (WSL) started only {_ago(up)} ago: Windows, WSL or Docker restarted. A run that was in progress was cut off; its folder has no DONE file and it will be redone.")
    hl = s.get("hostlog")
    if hl is None or now - hl["epoch"] > HOSTLOG_ALIVE_S:
        warnings.append("The host logger is NOT running. Open a second tmux window (Ctrl+b then c) and run: make hostlog   (it records CPU busy, steal and free memory every 30 s)")
    if s.get("stray"):
        warnings.append(f"Other containers are running and take CPU from the lab: docker stop {' '.join(s['stray'])}")
    if s.get("autoscalers"):
        warnings.append(f"A leftover autoscaler from an interrupted run exists ({', '.join(s['autoscalers'])}). The next run resets it by itself; `make recover` cleans it now.")

    unknown = [p for p in PHASE_ORDER if s.get("flagged", {}).get(p, []) is None]
    for p in unknown:
        warnings.append(f"The health flags of phase {p} could not be computed here. Run:  make health PHASE={p}")

    res = {"warnings": warnings, "after": []}

    if s["processes"]:
        res["headline"] = "A lab command is running right now. Do NOT start another one."
        res["steps"] = [("say", "Running: " + s["processes"][0]), ("say", "Watch it, or leave it alone. Reattach to the tmux session with:"), ("cmd", "tmux attach -t lab")]
        return res
    if not s["docker"]:
        res["headline"] = "Docker is not reachable."
        res["steps"] = [("say", "Start Docker Desktop and wait until its bottom-left corner says 'Engine running' (1-3 minutes after a restart). Then run:"), ("cmd", "make status")]
        return res
    deps = s.get("deployments", {})
    not_ready = (not s["node_running"]) or not deps or any(v is None or v[1] < max(v[0], 1) for v in deps.values())
    if not_ready:
        why = "the kind node container is not running" if not s["node_running"] else "llm / gateway / retriever are not all ready"
        res["headline"] = f"The cluster is not ready ({why}). This is the normal state after a restart."
        res["steps"] = [("say", "Repair it (takes 2-5 minutes; nothing is deleted), then check it:"), ("cmd", "make heal && make verify")]
        return res
    if not s["verified"]:
        res["headline"] = "The cluster is up but has not been verified since the last repair."
        res["steps"] = [("say", "Run the acceptance check (about 3 minutes):"), ("cmd", "make verify")]
        return res

    # work remaining, in plan order
    todo = [(p, s["phases"][p]) for p in PHASE_ORDER if s["phases"][p]["missing"]]
    flagged = {p: (s.get("flagged", {}).get(p) or []) for p in PHASE_ORDER}
    persistent = {p: (s.get("persistent", {}).get(p) or []) for p in PHASE_ORDER}
    handled_flag_phases: set[str] = set()   # phases whose flagged runs are the main step (not just an "after that" line)
    if todo:
        p, pr = todo[0]
        n = len(pr["missing"])
        cmds = phase_commands(p, pr["missing"])
        chain = "make gate && { " + "; ".join(cmds + ["make gate", f"make health PHASE={p}"]) + "; }"
        res["headline"] = f"{PHASE_TITLE[p]}: {n} run(s) still to do (about {n * MINUTES_PER_RUN / 60:.1f} hours). The lab is healthy, so start it:"
        res["steps"] = [
            ("say", "Before you leave the laptop: charger in, Windows Update paused, Chrome/ChatGPT/WhatsApp/Teams closed, OneDrive paused, then do not touch it. Run this (the gate comes first, the matrix only starts if the machine is healthy):"),
            ("cmd", chain),
        ]
        for q, qr in todo[1:]:
            res["after"].append(f"{PHASE_TITLE[q]}: {len(qr['missing'])} run(s) to do (about {len(qr['missing']) * MINUTES_PER_RUN / 60:.1f} hours): " + "; ".join(phase_commands(q, qr["missing"])))
    else:
        fresh = {p: [x for x in flagged[p] if x not in persistent[p]] for p in PHASE_ORDER}
        bad_phases = [p for p in PHASE_ORDER if fresh[p]]
        stuck_phases = [p for p in PHASE_ORDER if persistent[p]]
        if bad_phases:
            handled_flag_phases.update(bad_phases)
            n = sum(len(fresh[p]) for p in bad_phases)
            parts: list[str] = []
            for p in bad_phases:
                parts += [f"make rerun-bad PHASE={p}", "make gate"]
            chain = "make gate && { " + "; ".join(parts + ["make status"]) + "; }"
            res["headline"] = f"All planned runs exist, but {n} run(s) were measured on a slow host or are invalid (their names are listed under PROGRESS). Re-run exactly those:"
            res["steps"] = [("say", f"Hands-off, about {n * MINUTES_PER_RUN + 12 * (len(bad_phases) + 1):.0f} minutes including the gates (a re-run is refused unless a gate passed in the last 90 minutes). The last command shows whether anything is still flagged. "
                                    "If you ALREADY re-ran them once (before v2.5.9 that was not recorded) and they are still flagged, do not re-run again: use `make gate && make accept-flagged PHASE=B` (and A) instead."),
                            ("cmd", chain)]
        elif stuck_phases:
            handled_flag_phases.update(stuck_phases)
            n = sum(len(persistent[p]) for p in stuck_phases)
            cmds = [f"make accept-flagged PHASE={p}" for p in stuck_phases]
            res["headline"] = (f"{n} run(s) are STILL flagged after being re-run on a healthy machine. Same traffic, same flag: it is reproducible, not host noise, so re-running again will not change it.")
            res["steps"] = [("say", "Keep them (they stay in the analysis and are reported as 'flagged, reproducible'). About 12 minutes for the gate that vouches for the machine:"),
                            ("cmd", "make gate && { " + "; ".join(cmds + ["make status"]) + "; }")]
        elif unknown:
            res["headline"] = f"All planned runs exist, but the health check could not be computed for phase {unknown[0]}, so I cannot say they are clean."
            res["steps"] = [("say", "Look at the per-run table yourself (flags column: BAD, SLOW or INVALID mean re-run):"), ("cmd", f"make health PHASE={unknown[0]}")]
        else:
            res["headline"] = "All planned runs are finished and none is flagged. The data collection is DONE."
            res["steps"] = [("say", "Build the tables (REPORT.md and ROBUSTNESS.md), pack the results into one zip, copy it to Windows Downloads, and attach it to the chat:"),
                            ("cmd", "make analyze && make robust && make share"),
                            ("cmd", 'cp "$(ls -t ~/slo-loop-lab/results-share-*.zip | head -1)" /mnt/c/Users/Suhas/Downloads/')]
    # what comes after the main step
    for p in PHASE_ORDER:
        if not flagged[p]:
            continue
        nf = len([x for x in flagged[p] if x not in persistent[p]])
        if nf and p not in handled_flag_phases:
            res["after"].append(f"{nf} flagged run(s) in phase {p}: afterwards run  make gate && make rerun-bad PHASE={p}")
        # `make rerun-bad` skips runs that were already measured twice, so these are never re-run; they are kept instead (in every case)
        if persistent[p] and not (p in handled_flag_phases and nf == 0):
            res["after"].append(f"{len(persistent[p])} run(s) in phase {p} are still flagged after a re-run: afterwards run  make gate && make accept-flagged PHASE={p}")
    if todo:
        res["after"].append("Then: make share, and attach the zip to the chat so the hypotheses can be judged.")
    return res


# --------------------------------------------------------------------------------------------------------------------- output
def render(s: dict, d: dict | None = None) -> str:
    d = d or decide(s)
    now = s["now"]
    L: list[str] = []
    L.append(f"SLO-LOOP LAB STATUS   {_ist(now)}   (read-only: this command changes nothing)")
    L.append("")
    L.append("MACHINE")
    up = s.get("wsl_up_s")
    if up is not None:
        L.append(f"  Ubuntu (WSL) up since {_ist(now - up)} ({_ago(up)} ago)" + ("   <- RESTARTED recently" if up < RECENT_RESTART_S else ""))
    L.append(f"  Docker: {'reachable' if s['docker'] else 'NOT reachable'}" + (f"; kind node {s['node']}: {'running' if s['node_running'] else 'NOT running'}" if s["docker"] else ""))
    if s["node_running"]:
        parts = []
        for k, v in s["deployments"].items():
            parts.append(f"{k} {'?' if v is None else f'{v[1]}/{v[0]}'}")
        L.append("  Deployments ready/wanted: " + ", ".join(parts) + f"   verified: {'yes' if s['verified'] else 'NO'}")
    hl = s.get("hostlog")
    if hl is None:
        L.append("  Host logger: no results/hostlog.csv yet (run: make hostlog in a second tmux window)")
    else:
        age = now - hl["epoch"]
        tail = f"busy {hl['busy']:.0f}%" if hl["busy"] is not None else "busy ?"
        tail += f", steal {hl['steal']:.1f}%" if hl["steal"] is not None else ""
        tail += f", free {hl['mem_mb'] / 1000:.1f} GB" if hl["mem_mb"] is not None else ""
        L.append(f"  Host logger: {'running' if age <= HOSTLOG_ALIVE_S else 'NOT running'} (last line {_ago(age)} old: {tail})")
    L.append("")
    L.append("PROGRESS (target = what the plan needs)")
    for p in PHASE_ORDER:
        pr = s["phases"][p]
        fl = s["flagged"].get(p, [])
        L.append(f"  {PHASE_TITLE[p]:34s} {pr['done']:>3}/{pr['target']:<3} finished, {len(pr['missing']):>3} to run, {('?' if fl is None else len(fl)):>2} flagged (BAD/SLOW/INVALID)")
        if s.get("accepted", {}).get(p):
            L.append(f"      kept after a re-run (flag is reproducible, accepted with make accept-flagged): {s['accepted'][p]}")
        if fl:
            L.append("      flagged: " + "; ".join(fl[:6]) + (f"; ... (+{len(fl) - 6} more: make health PHASE={p})" if len(fl) > 6 else ""))
        if pr["interrupted"]:
            L.append(f"      started but never finished (will be redone): {', '.join(pr['interrupted'][:4])}" + (" ..." if len(pr["interrupted"]) > 4 else ""))
    lasts = [pr["last_done"] for pr in s["phases"].values() if pr["last_done"]]
    if lasts:
        name, t = max(lasts, key=lambda x: x[1])
        L.append(f"  Last finished run: {name} ({_ago(now - t)} ago)")
    lg = s.get("last_gate")
    if lg:
        sp = f", short-prompt median {lg['short_p50']:.2f} s" if lg.get("short_p50") is not None else ""
        L.append(f"  Last gate: {lg['name']} {lg['good_pct']:.1f}% good{sp}; {s['gate_ok'][1]}")
    else:
        L.append("  Last gate: none yet")
    if s.get("failures"):
        L.append("  Last lines of results/matrix-failures.log:")
        L += [f"      {ln[:150]}" for ln in s["failures"]]
    L.append("")
    if d["warnings"]:
        L.append("WARNINGS")
        L += [f"  !! {w}" for w in d["warnings"]]
        L.append("")
    L.append("NEXT")
    L.append(f"  {d['headline']}")
    for kind, text in d["steps"]:
        L.append(f"      $ {text}" if kind == "cmd" else f"  {text}")
    if d["after"]:
        L.append("")
        L.append("AFTER THAT")
        L += [f"  - {a}" for a in d["after"]]
    return "\n".join(L)


def main() -> int:
    print(render(gather()))
    return 0
