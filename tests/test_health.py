"""v2.5.4: per-run host-health table (`make health`), the host logger (`make hostlog`) and the burn-policy quiet wait.
Real incidents behind these tests (11 Nov 2026): the END gate of a pilot failed (90.7 % good, median TTFT 3.3 s vs 0.76 s at the start) and
nobody could say which of the 20 pilot runs were measured on the slow host; and s2_long_shift__burn started with 3 replicas because the
previous run's burn rate was still visible to KEDA. Offline; no cluster."""
from __future__ import annotations

import os
import subprocess
import textwrap

import pandas as pd

from conftest import ROOT
from lab import cli, gate, k8s, prom
from lab import config as cfg
from test_gate import _run, healthy, sick
from test_v25 import make_run

read = lambda p: (ROOT / p).read_text()  # noqa: E731


# ------------------------------------------------------------------------------------------- steady stretch + per-run health
def test_steady_stretch_ends_before_the_fault_or_the_first_load_change():
    inc = {"incidents": [{"start": 200, "end": 320}]}
    assert gate.steady_end("s1_burst", inc) == 190.0                       # incident at 200 s, load step at 200 s
    assert gate.steady_end("s8_ramp", {"incidents": [{"start": 240, "end": 480}]}) == 115.0   # ramp starts at 120 s
    assert gate.steady_end("s0_baseline", {"incidents": []}) is None      # whole run
    assert gate.steady_end("no_such_scenario", inc) == 190.0              # unknown scenario: fall back to the incident start


def _bad_before_150(t):
    return t < 150 and int(t * 5) % 4 == 0


def test_run_health_measures_only_the_steady_stretch(tmp_path):
    good = make_run(tmp_path, name="s1_burst__static1__r1", incident=(200, 320), bad=lambda t: False)
    # everything bad AFTER the steady stretch must not count
    late = make_run(tmp_path, name="s1_burst__cpu__r1", incident=(200, 320), bad=lambda t: 200 <= t < 320)
    sick = make_run(tmp_path, name="s1_burst__burn__r1", incident=(200, 320), bad=_bad_before_150)
    h_good, h_late, h_sick = (gate.run_health(d) for d in (good, late, sick))
    assert h_good["pre_good_pct"] == 100.0 and h_good["short_p50"] == 0.5 and h_good["pre_n"] > 100
    assert h_late["pre_good_pct"] == 100.0 and h_late["good_pct"] < 100.0     # whole-run good% is lower, steady stretch is clean
    assert h_sick["pre_good_pct"] < 95.0


def test_flags_mark_invalid_bad_and_slow_runs():
    base = {"valid": True, "pre_n": 40, "pre_good_pct": 100.0, "short_n": 20, "short_p50": 0.4}
    rows = [dict(base, name="ok"), dict(base, name="bad", pre_good_pct=90.0), dict(base, name="slow", short_p50=1.0),
            dict(base, name="inv", valid=False), dict(base, name="few", pre_n=5, pre_good_pct=60.0, short_n=3, short_p50=9.0)]
    gate.flag_runs(rows)
    got = {r["name"]: r["flags"] for r in rows}
    assert got == {"ok": [], "bad": ["BAD"], "slow": ["SLOW"], "inv": ["INVALID"], "few": []}   # too few requests: no verdict


def test_health_command_prints_a_table_with_ist_times_and_flags(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "results" / "runs"
    b = runs / "B"
    b.mkdir(parents=True)
    make_run(b, name="s1_burst__static1__r1", incident=(200, 320))
    make_run(b, name="s1_burst__cpu__r1", incident=(200, 320), bad=_bad_before_150)
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    assert cli.main(["health", "--phase", "B"]) == 0
    out = capsys.readouterr().out
    assert "s1_burst__static1__r1" in out and "s1_burst__cpu__r1" in out
    assert "01-Jan 05:46" in out                                    # start_epoch 1000 s UTC shown in India time (+5:30)
    assert [ln for ln in out.splitlines() if "s1_burst__cpu__r1" in ln][0].rstrip().endswith("BAD")
    assert "host busy%" not in out                                  # no host log yet
    assert "make hostlog" in read("Makefile")


def test_health_with_nothing_to_show_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "RUNS_DIR", tmp_path / "results" / "runs")
    assert cli.main(["health"]) == 0
    assert "no finished runs" in capsys.readouterr().out


def test_health_joins_the_host_log_by_time(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "results" / "runs"
    (runs / "B").mkdir(parents=True)
    make_run(runs / "B", name="s1_burst__static1__r1", incident=(200, 320))       # start_epoch 1000, end 1600
    log = pd.DataFrame({"epoch": [900, 1100, 1300, 1500, 1700], "load1": 3.0, "cpu_busy_pct": [10, 90, 80, 70, 5],
                        "cpu_steal_pct": [0, 14, 12, 10, 0], "mem_avail_mb": [3000, 2500, 2400, 2600, 3000],
                        "swap_used_mb": 0, "psi_cpu_avg10": "", "top_container": "kind-control-plane 50%"})
    log.to_csv(tmp_path / "results" / gate.HOSTLOG_NAME, index=False)
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    cli.main(["health", "--phase", "B"])
    out = capsys.readouterr().out
    assert "host busy%" in out and "steal%" in out and "min free MB" in out
    row = [ln for ln in out.splitlines() if "s1_burst__static1__r1" in ln][0]
    assert " 80 " in row and " 12.0 " in row and " 2400 " in row    # mean busy 80, mean steal 12, lowest free memory 2400


# ------------------------------------------------------------------------------------------- host logger
def test_hostlog_writes_a_header_and_one_complete_line(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "docker").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        echo "slo-loop-control-plane 12.50%"
        echo "kind-control-plane 57.31%"
        """))
    (stubs / "docker").chmod(0o755)
    out = tmp_path / "hostlog.csv"
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "HOSTLOG_FILE": str(out), "HOSTLOG_ONCE": "1", "HOSTLOG_SAMPLE_S": "1"}
    r = subprocess.run(["bash", str(ROOT / "scripts" / "hostlog.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    lines = out.read_text().strip().splitlines()
    assert lines[0] == "epoch,load1,cpu_busy_pct,cpu_steal_pct,mem_avail_mb,swap_used_mb,psi_cpu_avg10,top_container"
    assert len(lines) == 2
    cols = lines[1].split(",")
    assert len(cols) == 8 and cols[0].isdigit() and float(cols[1]) >= 0 and float(cols[2]) >= 0 and float(cols[3]) >= 0
    assert cols[7] == "kind-control-plane 57%"                       # busiest container
    # a second run appends (no second header)
    subprocess.run(["bash", str(ROOT / "scripts" / "hostlog.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert len(out.read_text().strip().splitlines()) == 3


def test_hostlog_survives_docker_being_down(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "docker").write_text("#!/usr/bin/env bash\nexit 1\n")
    (stubs / "docker").chmod(0o755)
    out = tmp_path / "h.csv"
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "HOSTLOG_FILE": str(out), "HOSTLOG_ONCE": "1", "HOSTLOG_SAMPLE_S": "1"}
    assert subprocess.run(["bash", str(ROOT / "scripts" / "hostlog.sh")], env=env, capture_output=True, text=True, timeout=60).returncode == 0
    cols = out.read_text().strip().splitlines()[1].split(",")
    assert len(cols) == 8 and cols[7] == ""


# ------------------------------------------------------------------------------------------- burn quiet wait
def _prom(monkeypatch, values):
    seq = list(values)
    sleeps = []
    monkeypatch.setattr(prom, "instant", lambda q, timeout_s=10.0: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(k8s.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_burn_quiet_waits_until_two_calm_polls_in_a_row(monkeypatch):
    sleeps = _prom(monkeypatch, [14.0, 6.0, 0.4, 3.0, 0.3, 0.2])
    assert k8s.wait_burn_quiet(timeout_s=600, poll_s=5) == 0.2
    assert len(sleeps) == 5                                          # 14, 6, .4(calm), 3 (resets), .3(calm), .2(calm)


def test_burn_quiet_is_best_effort_when_prometheus_is_down(monkeypatch):
    sleeps = _prom(monkeypatch, [None])
    assert k8s.wait_burn_quiet() is None and sleeps == []


def test_burn_quiet_gives_up_with_a_message_instead_of_hanging(monkeypatch):
    _prom(monkeypatch, [9.0])
    said = []
    assert k8s.wait_burn_quiet(timeout_s=0, say=said.append) == 9.0
    assert said and "starting anyway" in said[0] and "flagged invalid" in said[0]


def test_only_the_burn_policy_waits_for_a_quiet_burn_rate_and_it_waits_before_keda_is_switched_on(monkeypatch):
    def run(policy):
        order = []
        monkeypatch.setattr(k8s, "ensure_release_deployed", lambda: None)
        monkeypatch.setattr(k8s, "helm_set_policy", lambda p: order.append(f"helm:{p}"))
        monkeypatch.setattr(k8s, "settle_one_replica", lambda *a, **k: None)
        monkeypatch.setattr(k8s, "wait_ready", lambda *a, **k: None)
        monkeypatch.setattr(k8s, "restart_all", lambda *a, **k: order.append("restart"))
        monkeypatch.setattr(k8s, "wait_burn_quiet", lambda *a, **k: order.append("quiet"))
        k8s.set_policy(policy)
        return order

    assert run("burn") == ["helm:static1", "restart", "quiet", "helm:burn"]
    assert "quiet" not in run("queue") and "quiet" not in run("cpu") and "quiet" not in run("static3")


def test_prom_instant_returns_none_when_unreachable_and_the_max_otherwise(monkeypatch):
    import httpx

    class R:
        def __init__(self, result): self._r = result
        def raise_for_status(self): pass
        def json(self): return {"data": {"result": self._r}}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R([{"value": [1, "2.5"]}, {"value": [1, "NaN"]}, {"value": [1, "7"]}]))
    assert prom.instant("q") == 7.0
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R([]))
    assert prom.instant("q") == 0.0

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert prom.instant("q") is None


def test_docs_and_make_targets_exist():
    mk = read("Makefile")
    assert "$(LAB) health --phase $(PHASE)" in mk and "bash scripts/hostlog.sh" in mk
    assert " health " in mk.split("\n")[7] and " hostlog " in mk.split("\n")[7]
    t = read("docs/TROUBLESHOOTING.md")
    assert "make health" in t and "make hostlog" in t and "burn" in t and "t0 were 3" in t


# ------------------------------------------------------------------------------------------- every command's --help must work
# Real incident (11 Nov): `help="... good%/TTFT ..."` made EVERY `slo-lab` command crash with "ValueError: badly formed help string" on
# Python 3.14, because argparse treats a bare % in a help string as a format directive (3.14 checks it while the parser is built; older
# versions only when --help is printed). 23 tests failed on the user's machine and `make gate` could not even start.
import re  # noqa: E402

import pytest  # noqa: E402

_COMMANDS = re.findall(r'sub\.add_parser\("([a-z-]+)"', read("lab/cli.py"))


def test_there_are_commands_to_check():
    assert len(_COMMANDS) >= 15 and {"gate", "health", "run", "matrix", "summarize"} <= set(_COMMANDS)


@pytest.mark.parametrize("cmd", [None] + _COMMANDS)
def test_help_of_every_command_can_be_printed(cmd, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"] if cmd is None else [cmd, "--help"])
    assert e.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_no_help_string_has_a_bare_percent_sign():
    bad = []
    for n, line in enumerate(read("lab/cli.py").splitlines(), 1):
        for m in re.finditer(r'help\s*=\s*f?"((?:[^"\\]|\\.)*)"', line):
            if re.search(r"%(?!\()", m.group(1).replace("%%", "")):   # "%%" is the correct way to write a percent sign
                bad.append((n, m.group(1)[:60]))
    assert bad == [], f"write %% instead of % inside argparse help strings: {bad}"



# ------------------------------------------------------------------------------------------- v2.5.6: re-run flagged runs, gate freshness
from lab import runner  # noqa: E402


def _gate_run(runs, n, bad=lambda t: False):
    g = runs / "G"
    g.mkdir(parents=True, exist_ok=True)
    return make_run(g, name=f"s0_baseline__static1__r{n}", incident=None, bad=bad, rps=0.2, duration=600)   # start_epoch 1000


def test_recent_gate_ok_needs_a_passing_and_fresh_gate(tmp_path):
    runs = tmp_path / "runs"
    ok, msg = gate.recent_gate_ok(runs)
    assert not ok and "make gate" in msg                                              # no gate at all
    _gate_run(runs, 1)
    assert gate.recent_gate_ok(runs, now=1000 + 600)[0] is True                       # passed 10 min ago
    ok, msg = gate.recent_gate_ok(runs, now=1000 + 3 * 3600)
    assert not ok and "minutes old" in msg                                            # passed, but too long ago
    _gate_run(runs, 2, bad=lambda t: int(t / 5) % 9 == 0)                             # a newer gate FAILED: the older pass no longer counts
    ok, msg = gate.recent_gate_ok(runs, now=1000 + 600)
    assert not ok and "FAILED" in msg and "r2" in msg


def test_gate_verdict_and_history_show_the_short_prompt_median(tmp_path):
    d = _run(tmp_path, healthy)
    v = gate.evaluate(d, 97.0)
    assert v["short_p50"] == 0.5
    assert "short-prompt median 0.50 s" in gate.format_verdict(v, "G/x", 97.0)
    runs = tmp_path / "runs"
    _gate_run(runs, 1)
    assert "short p50" in gate.format_history(gate.history(runs), 97.0)
    sick_txt = gate.format_verdict(gate.evaluate(_run(tmp_path / "s", sick), 97.0), "G/x", 97.0)
    assert "do not touch the laptop" in sick_txt and "CLOSE Chrome" in sick_txt


def _rerun_env(tmp_path, monkeypatch, gate_ok=True):
    runs = tmp_path / "results" / "runs"
    b = runs / "B"
    b.mkdir(parents=True)
    make_run(b, name="s1_burst__static1__r1", incident=(200, 320))                                          # healthy
    make_run(b, name="s2_long_shift__cpu__r1", incident=(200, 320), bad=_bad_before_150, policy="cpu")       # BAD
    d = make_run(b, name="s3_cpu_stress__burn__r1", incident=(200, 320), policy="burn")                      # INVALID
    import json
    meta = json.loads((d / "meta.json").read_text())
    meta.update(valid=False, invalid_reasons=["replicas at t0 were 3, expected 1"])
    (d / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    calls = []
    monkeypatch.setattr(runner, "run_one", lambda phase, sc, pol, rep, force=False, **kw: calls.append((phase, sc, pol, rep, force)))
    if gate_ok is not None:
        monkeypatch.setattr(gate, "recent_gate_ok", lambda *a, **k: (gate_ok, "latest gate G/x passed 5 minutes ago" if gate_ok else "the latest gate FAILED"))
    return calls


def test_rerun_reruns_exactly_the_flagged_runs_with_force(tmp_path, monkeypatch, capsys):
    calls = _rerun_env(tmp_path, monkeypatch)
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 0
    assert sorted(calls) == [("B", "s2_long_shift", "cpu", 1, True), ("B", "s3_cpu_stress", "burn", 1, True)]
    out = capsys.readouterr().out
    assert "re-running 2 flagged run(s)" in out and "make gate" in out


def test_rerun_is_refused_without_a_fresh_passing_gate(tmp_path, monkeypatch, capsys):
    calls = _rerun_env(tmp_path, monkeypatch, gate_ok=False)
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 1
    assert calls == [] and "REFUSED" in capsys.readouterr().err
    assert cli.main(["health", "--phase", "B", "--rerun", "--no-gate-check"]) == 0 and len(calls) == 2     # explicit override


def test_rerun_needs_a_real_phase_and_has_nothing_to_do_on_a_clean_phase(tmp_path, monkeypatch, capsys):
    calls = _rerun_env(tmp_path, monkeypatch)
    assert cli.main(["health", "--rerun"]) == 2 and cli.main(["health", "--phase", "G", "--rerun"]) == 2 and calls == []
    clean = tmp_path / "clean" / "runs"
    (clean / "B").mkdir(parents=True)
    make_run(clean / "B", name="s1_burst__static1__r1", incident=(200, 320))
    monkeypatch.setattr(cfg, "RUNS_DIR", clean)
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 0 and calls == []
    assert "nothing to re-run" in capsys.readouterr().out


def test_rerun_stops_and_names_the_run_that_failed(tmp_path, monkeypatch, capsys):
    calls = _rerun_env(tmp_path, monkeypatch)

    def boom(phase, sc, pol, rep, force=False, **kw):
        calls.append(sc)
        raise k8s.LabNotReady("llm did not become ready")

    monkeypatch.setattr(runner, "run_one", boom)
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 1
    err = capsys.readouterr().err
    assert len(calls) == 1 and "FAILED" in err and "llm did not become ready" in err and "make recover" in err


def test_rerun_bad_make_target_and_docs():
    mk = read("Makefile")
    assert "rerun-bad: require-verified" in mk and "health --phase $(PHASE) --rerun" in mk and " rerun-bad " in mk.split("\n")[7]
    t = read("docs/TROUBLESHOOTING.md")
    assert "make rerun-bad" in t and "hands-off" in t.lower()
    assert "make rerun-bad" in read("README.md")


# ------------------------------------------------------------------------------------------- v2.5.9: accept flags that survive a re-run
# Real incident (17 Nov): 16 runs were flagged, all 16 were re-run on a healthy machine (gate PASS 100 %, short median 0.59 s), and 7 were flagged
# AGAIN with the same flag. Same traffic (the seed depends on scenario + rep only) on a healthy host and the same flag = a property of that
# draw, not host noise. `make status` kept saying "re-run", which would have cost 2 more hours for the same result.
def test_run_health_reads_how_often_a_run_was_measured(tmp_path):
    d = make_run(tmp_path, name="s2_long_shift__cpu__r1", incident=(200, 320))
    assert gate.run_health(d)["attempts"] == 1                                              # runs from before v2.5.9 have no field
    import json
    meta = json.loads((d / "meta.json").read_text())
    meta["attempts"] = 3
    (d / "meta.json").write_text(json.dumps(meta))
    assert gate.run_health(d)["attempts"] == 3


def test_accept_keeps_a_bad_run_but_never_an_invalid_one(tmp_path, monkeypatch, capsys):
    # B: healthy s1_burst, BAD s2_long_shift cpu, INVALID s3_cpu_stress burn
    calls = _rerun_env(tmp_path, monkeypatch)
    assert cli.main(["health", "--phase", "B", "--accept"]) == 0
    out = capsys.readouterr()
    assert "kept 1 run(s)" in out.out and "s2_long_shift__cpu__r1" in out.out and "[BAD]" in out.out
    assert "NOT accepted" in out.err and "s3_cpu_stress__burn__r1" in out.err
    acc = gate.load_accepted(cfg.RUNS_DIR.parent / gate.ACCEPT_NAME)
    assert acc == {("B", "s2_long_shift__cpu__r1"): {"BAD"}}
    rows = gate.health_rows(cfg.RUNS_DIR, "B")
    gate.flag_runs(rows)
    by = {r["name"]: r for r in rows}
    assert by["s2_long_shift__cpu__r1"]["flags"] == [] and by["s2_long_shift__cpu__r1"]["accepted_flags"] == ["BAD"]
    assert by["s3_cpu_stress__burn__r1"]["flags"] == ["INVALID"]
    table = gate.format_health(rows, hostlog_present=False)
    assert "accepted:BAD" in table
    # an accepted run is no longer re-run; the invalid one still is
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 0
    assert [(c[2], c[3]) for c in calls] == [("burn", 1)]


def test_accept_is_refused_without_a_fresh_passing_gate_and_needs_a_real_phase(tmp_path, monkeypatch, capsys):
    _rerun_env(tmp_path, monkeypatch, gate_ok=False)
    assert cli.main(["health", "--phase", "B", "--accept"]) == 1
    assert "REFUSED" in capsys.readouterr().err and not (cfg.RUNS_DIR.parent / gate.ACCEPT_NAME).exists()
    assert cli.main(["health", "--phase", "G", "--accept"]) == 2
    assert cli.main(["health", "--accept"]) == 2
    assert cli.main(["health", "--phase", "B", "--accept", "--rerun"]) == 2
    assert cli.main(["health", "--phase", "B", "--accept", "--no-gate-check"]) == 0          # explicit override, like --rerun


def test_accept_with_nothing_flagged_says_so(tmp_path, monkeypatch, capsys):
    _rerun_env(tmp_path, monkeypatch)
    cli.main(["health", "--phase", "B", "--accept"])
    capsys.readouterr()
    runs = cfg.RUNS_DIR
    import shutil
    shutil.rmtree(runs / "B" / "s3_cpu_stress__burn__r1")                                      # drop the INVALID run: only accepted flags remain
    assert cli.main(["health", "--phase", "B", "--accept"]) == 0
    assert "nothing to accept" in capsys.readouterr().out


def test_accept_target_and_docs():
    mk = read("Makefile").splitlines()
    assert any(ln.startswith("accept-flagged:") for ln in mk) and "accept-flagged" in " ".join(mk[:12])
    assert "make accept-flagged" in read("README.md") and "make accept-flagged" in read("docs/TROUBLESHOOTING.md")


def test_rerun_skips_a_run_that_was_already_measured_twice_but_never_an_invalid_one(tmp_path, monkeypatch, capsys):
    calls = _rerun_env(tmp_path, monkeypatch)
    import json
    for name in ("s2_long_shift__cpu__r1", "s3_cpu_stress__burn__r1"):            # both measured twice; only the BAD one is reproducible
        p = cfg.RUNS_DIR / "B" / name / "meta.json"
        m = json.loads(p.read_text()); m["attempts"] = 2; p.write_text(json.dumps(m))
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 0
    out = capsys.readouterr().out
    assert [(c[2], c[3]) for c in calls] == [("burn", 1)]                              # INVALID is re-run no matter how often it was measured
    assert "SKIPPING 1 run(s)" in out and "s2_long_shift__cpu__r1" in out and "make accept-flagged PHASE=B" in out
    calls.clear()
    meta = cfg.RUNS_DIR / "B" / "s3_cpu_stress__burn__r1" / "meta.json"
    m = json.loads(meta.read_text()); m["valid"] = True; m["invalid_reasons"] = []; meta.write_text(json.dumps(m))
    assert cli.main(["health", "--phase", "B", "--rerun"]) == 0
    assert calls == [] and "nothing left to re-run" in capsys.readouterr().out


def test_accepting_a_run_in_one_phase_never_accepts_the_same_name_in_the_other_phase(tmp_path, monkeypatch, capsys):
    """Real bug (18 Nov): s2_long_shift__static1__r2 exists in Phase A AND Phase B. v2.5.9 keyed the accepted-flags file by name only, so keeping the
    Phase B run also kept the Phase A run without any Phase A decision (`make accept-flagged PHASE=A` said 'kept 2' while `make status` counted 3)."""
    runs = tmp_path / "results" / "runs"
    for ph in ("A", "B"):
        (runs / ph).mkdir(parents=True)
        make_run(runs / ph, name="s2_long_shift__static1__r2", incident=(200, 320), bad=_bad_before_150)
        make_run(runs / ph, name="s1_burst__static1__r1", incident=(200, 320))
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    monkeypatch.setattr(gate, "recent_gate_ok", lambda *a, **k: (True, "latest gate passed 5 minutes ago"))
    for ph in ("A", "B"):                                  # make_run writes phase "A" into meta: make the meta agree with the directory
        import json
        for d in (runs / ph).iterdir():
            m = json.loads((d / "meta.json").read_text()); m["phase"] = ph; (d / "meta.json").write_text(json.dumps(m))
    assert cli.main(["health", "--phase", "B", "--accept"]) == 0
    capsys.readouterr()
    by = {}
    for ph in ("A", "B"):
        rows = gate.health_rows(runs, ph)
        gate.flag_runs(rows)
        by[ph] = {r["name"]: r for r in rows}
    assert by["B"]["s2_long_shift__static1__r2"]["flags"] == [] and by["B"]["s2_long_shift__static1__r2"]["accepted_flags"]
    assert by["A"]["s2_long_shift__static1__r2"]["flags"] and not by["A"]["s2_long_shift__static1__r2"]["accepted_flags"]   # still needs its own decision
    assert cli.main(["health", "--phase", "A", "--accept"]) == 0
    assert "kept 1 run(s)" in capsys.readouterr().out
