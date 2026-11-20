"""`make status`: the one-screen "where am I, what next" command (read-only). Added after a restart left the user unable to tell
which of the 40 overnight runs had finished."""
from __future__ import annotations

import subprocess

import pytest
from conftest import ROOT
from test_health import _rerun_env

from lab import cli, gate, status
from lab import config as cfg

NOW = 1_800_000_000.0


def _progress(phase="B", done=None, missing=None, interrupted=None, last=None):
    return {"phase": phase, "target": 60, "done": done if done is not None else 60, "missing": missing or [],
            "interrupted": interrupted or [], "last_done": last}


def _state(**over):
    s = {
        "now": NOW, "wsl_up_s": 7200.0, "processes": [], "node": "slo-loop-control-plane", "docker": True, "node_running": True,
        "stray": [], "deployments": {"llm": (1, 1), "gateway": (1, 1), "retriever": (1, 1)}, "autoscalers": [], "verified": True,
        "hostlog": {"epoch": NOW - 10, "busy": 7.0, "steal": 0.0, "mem_mb": 8200.0, "top": "slo-loop-control-plane 40%"},
        "last_gate": {"name": "s0_baseline__static1__r8", "good_pct": 100.0, "short_p50": 0.62}, "gate_ok": (True, "latest gate passed 20 minutes ago"),
        "phases": {"B": _progress("B"), "A": _progress("A")}, "flagged": {"B": [], "A": []}, "failures": [],
    }
    s.update(over)
    return s


def _cmds(d):
    return [t for k, t in d["steps"] if k == "cmd"]


# ------------------------------------------------------------------------------------------------------------ the plan
def test_target_counts_match_the_plan():
    b = status.target_jobs("B")
    assert len(b) == 4 * 5 * 3 == 60
    a = status.target_jobs("A")
    assert len(a) == 7 * 6 + 13 == 55
    assert sum(1 for sc, _, _ in a if sc == "s7_slow_burn") == 13
    assert max(r for sc, _, r in a if sc == "s0_baseline") == 6


def test_netem_scenarios_are_left_out_when_this_machine_cannot_do_netem():
    with_netem = status.target_jobs("A", netem_ok=True)
    without = status.target_jobs("A", netem_ok=False)
    assert len(with_netem) == 55
    assert len(without) == 55 - 6 and all(sc != "s6_netem_delay" for sc, _, _ in without)


def test_progress_counts_done_missing_and_interrupted(tmp_path):
    runs = tmp_path / "runs"
    pdir = runs / "B"
    for name in ("s1_burst__static1__r1", "s1_burst__cpu__r1"):
        (pdir / name).mkdir(parents=True)
        (pdir / name / "DONE").write_text("ok\n")
    (pdir / "s2_long_shift__queue__r2").mkdir()          # started, never finished (restart)
    p = status.progress(runs, "B")
    assert p["target"] == 60 and p["done"] == 2 and len(p["missing"]) == 58
    assert p["interrupted"] == ["s2_long_shift__queue__r2"]
    assert ("s2_long_shift", "queue", 2) in p["missing"] and ("s1_burst", "cpu", 1) not in p["missing"]
    assert p["last_done"][0] in ("s1_burst__static1__r1", "s1_burst__cpu__r1")


def test_progress_of_a_phase_that_does_not_exist_yet(tmp_path):
    p = status.progress(tmp_path / "runs", "A")
    assert p["done"] == 0 and len(p["missing"]) == 55 and p["interrupted"] == [] and p["last_done"] is None


# ------------------------------------------------------------------------------------------------------------ the decision
def test_a_running_lab_command_blocks_everything_else():
    d = status.decide(_state(processes=["slo-lab matrix --phase B --reps 3"], docker=False))
    assert "Do NOT start another" in d["headline"]
    assert _cmds(d) == ["tmux attach -t lab"]


def test_docker_down_means_start_docker_then_status_again():
    d = status.decide(_state(docker=False, node_running=False, deployments={}))
    assert "Docker is not reachable" in d["headline"] and _cmds(d) == ["make status"]


@pytest.mark.parametrize("over", [
    {"node_running": False, "deployments": {}},
    {"deployments": {"llm": (1, 0), "gateway": (1, 1), "retriever": (1, 1)}},
    {"deployments": {"llm": None, "gateway": (1, 1), "retriever": (1, 1)}},
])
def test_cluster_not_ready_means_heal_and_verify(over):
    d = status.decide(_state(**over))
    assert "cluster is not ready" in d["headline"] and _cmds(d) == ["make heal && make verify"]


def test_not_verified_means_verify():
    d = status.decide(_state(verified=False))
    assert _cmds(d) == ["make verify"]


def test_missing_phase_b_runs_give_the_hands_off_chain():
    missing = [("s1_burst", "cpu", r) for r in (2, 3)]
    d = status.decide(_state(phases={"B": _progress("B", done=58, missing=missing), "A": _progress("A")}))
    assert _cmds(d) == ["make gate && { make phaseB REPS=3; make gate; make health PHASE=B; }"]
    assert "2 run(s)" in d["headline"]


def test_phase_a_commands_split_the_slow_burn_scenario():
    both = [("s0_baseline", "static1", 4), ("s7_slow_burn", "static1", 11)]
    only_s7 = [("s7_slow_burn", "static1", 11)]
    only_other = [("s0_baseline", "static1", 4)]
    assert status.phase_commands("A", both) == ["make phaseA REPS=6", ".venv/bin/slo-lab matrix --phase A --reps 13 --only s7_slow_burn"]
    assert status.phase_commands("A", only_s7) == [".venv/bin/slo-lab matrix --phase A --reps 13 --only s7_slow_burn"]
    assert status.phase_commands("A", only_other) == ["make phaseA REPS=6"]
    d = status.decide(_state(phases={"B": _progress("B"), "A": _progress("A", done=50, missing=both)}))
    chain = _cmds(d)[0]
    assert chain.startswith("make gate && { make phaseA REPS=6; .venv/bin/slo-lab matrix") and chain.endswith("make gate; make health PHASE=A; }")


def test_phase_b_goes_first_and_phase_a_is_listed_for_later():
    d = status.decide(_state(phases={"B": _progress("B", done=0, missing=[("s1_burst", "cpu", 1)] * 3), "A": _progress("A", done=0, missing=[("s0_baseline", "static1", 4)])}))
    assert "PHASE=B" in _cmds(d)[0] and any("make phaseA REPS=6" in a for a in d["after"])


def test_flagged_runs_are_re_run_when_nothing_is_missing():
    d = status.decide(_state(flagged={"B": ["s1_burst__static1__r1 [BAD]"], "A": []}))
    assert _cmds(d) == ["make gate && { make rerun-bad PHASE=B; make gate; make status; }"]
    assert "1 run(s) were measured on a slow host" in d["headline"]


def test_flagged_runs_in_both_phases_share_one_hands_off_chain_with_a_gate_between():
    d = status.decide(_state(flagged={"B": ["a [BAD]"], "A": ["b [SLOW]", "c [INVALID]"]}))
    assert _cmds(d) == ["make gate && { make rerun-bad PHASE=B; make gate; make rerun-bad PHASE=A; make gate; make status; }"]
    assert "3 run(s)" in d["headline"] and d["after"] == []
    assert "about 69 minutes" in " ".join(t for k, t in d["steps"] if k == "say")


def test_everything_done_and_clean_means_share():
    d = status.decide(_state())
    assert "DONE" in d["headline"] and _cmds(d)[0] == "make analyze && make robust && make share" and "results-share" in _cmds(d)[1]


def test_flagged_runs_in_another_phase_are_listed_after_the_main_step():
    d = status.decide(_state(phases={"B": _progress("B", done=0, missing=[("s1_burst", "cpu", 1)]), "A": _progress("A")}, flagged={"B": [], "A": ["a", "b"]}))
    assert any("2 flagged run(s) in phase A" in a and "rerun-bad PHASE=A" in a for a in d["after"])


def test_warnings_recent_restart_dead_logger_stray_containers_leftover_autoscaler():
    d = status.decide(_state(wsl_up_s=300.0, hostlog=None, stray=["kind-control-plane"], autoscalers=["scaledobject.keda.sh/llm"]))
    w = " | ".join(d["warnings"])
    assert "started only 5 min ago" in w and "host logger is NOT running" in w and "docker stop kind-control-plane" in w and "leftover autoscaler" in w
    old_log = status.decide(_state(hostlog={"epoch": NOW - 600, "busy": 1.0, "steal": 0.0, "mem_mb": 1.0, "top": ""}))
    assert any("host logger is NOT running" in x for x in old_log["warnings"])
    assert status.decide(_state())["warnings"] == []


# ------------------------------------------------------------------------------------------------------------ probes and output
def test_hostlog_tail_reads_the_newest_line(tmp_path):
    f = tmp_path / "hostlog.csv"
    f.write_text("epoch,load1,cpu_busy_pct,cpu_steal_pct,mem_avail_mb,swap_used_mb,psi_cpu_avg10,top_container\n"
                 "1794470488,0.5,6.0,0.0,8281,0,0.01,slo-loop-control-plane 38%\n1794470498,0.61,7.6,0.0,8274,0,0.02,slo-loop-control-plane 43%\n")
    t = status._hostlog_tail(f)
    assert t["epoch"] == 1794470498.0 and t["busy"] == 7.6 and t["steal"] == 0.0 and t["mem_mb"] == 8274.0 and "43%" in t["top"]
    assert status._hostlog_tail(tmp_path / "missing.csv") is None
    (tmp_path / "empty.csv").write_text("epoch,load1\n")
    assert status._hostlog_tail(tmp_path / "empty.csv") is None


def test_lab_processes_only_reports_commands_that_would_clash(monkeypatch):
    listing = "\n".join([
        "101 /h/.venv/bin/python /h/.venv/bin/slo-lab matrix --phase B --reps 3",
        "102 /h/.venv/bin/python /h/.venv/bin/slo-lab gate --history",
        "103 /h/.venv/bin/python /h/.venv/bin/slo-lab health --phase B",
        "104 /h/.venv/bin/python /h/.venv/bin/slo-lab health --phase B --rerun",
        "105 /h/.venv/bin/python /h/.venv/bin/slo-lab status",
        "106 /h/.venv/bin/python /h/.venv/bin/slo-lab gate",
        "107 /h/.venv/bin/python /h/.venv/bin/slo-lab matrix --phase B --reps 3",
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=listing, stderr=""))
    got = status._lab_processes()
    assert got == ["slo-lab matrix --phase B --reps 3", "slo-lab health --phase B --rerun", "slo-lab gate"]


def test_lab_processes_survives_a_missing_pgrep(monkeypatch):
    def boom(*a, **k):
        raise OSError("no pgrep")
    monkeypatch.setattr(subprocess, "run", boom)
    assert status._lab_processes() == []


def test_gather_with_docker_down_never_crashes_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "running_containers", lambda: None)
    monkeypatch.setattr(status, "_lab_processes", lambda: [])
    runs = tmp_path / "results" / "runs"
    runs.mkdir(parents=True)
    s = status.gather(runs, now=NOW)
    assert s["docker"] is False and s["node_running"] is False and s["phases"]["B"]["done"] == 0
    out = status.render(s)
    assert "Docker: NOT reachable" in out and "Docker is not reachable" in out and "$ make status" in out


def test_render_shows_progress_time_in_ist_and_the_next_command():
    s = _state(phases={"B": _progress("B", done=20, missing=[("s1_burst", "cpu", 2)] * 40, interrupted=["s1_burst__cpu__r2"], last=("s3_cpu_stress__burn__r1", NOW - 3 * 86400)),
                       "A": _progress("A", done=31, missing=[("s0_baseline", "static1", 4)] * 24)},
               flagged={"B": ["x"], "A": []})
    out = status.render(s)
    assert " IST" in out and "20/60" in out and "40 to run" in out and "31/60" in out
    assert "never finished (will be redone): s1_burst__cpu__r2" in out and "3.0 days ago" in out
    assert "make gate && { make phaseB REPS=3; make gate; make health PHASE=B; }" in out and "AFTER THAT" in out
    assert "short-prompt median 0.62 s" in out and "Host logger: running" in out


def test_ago_and_ist_helpers():
    assert status._ago(30) == "30 s" and status._ago(600) == "10 min" and status._ago(7200) == "2.0 h" and status._ago(3 * 86400) == "3.0 days"
    assert status._ist(1794470508) == "Thu 12-Nov 13:31 IST"


def test_status_cli_prints_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(status, "gather", lambda *a, **k: _state())
    assert cli.main(["status"]) == 0
    assert "SLO-LOOP LAB STATUS" in capsys.readouterr().out


def test_status_is_wired_into_make_and_docs():
    mk = (ROOT / "Makefile").read_text().splitlines()
    assert "status" in mk[7].split() and any(ln.startswith("status:") for ln in mk)
    assert "make status" in (ROOT / "README.md").read_text()
    assert "make status" in (ROOT / "docs" / "TROUBLESHOOTING.md").read_text()
    assert cfg.RUNS_DIR.name == "runs"


# ------------------------------------------------------------------------------------------------------------ v2.5.8: flags must be REAL
# Real incident (16 Nov): v2.5.7 read r["flags"] from gate.health_rows(), which does not set them (format_health() does), the KeyError was
# swallowed by a blanket except, and `make status` reported "0 flagged" for every phase while `make health` showed BAD runs.
def test_gather_reports_the_runs_that_make_health_flags(tmp_path, monkeypatch):
    _rerun_env(tmp_path, monkeypatch)                     # B: one healthy run, one BAD run, one INVALID run
    monkeypatch.setattr(gate, "running_containers", lambda: None)
    monkeypatch.setattr(status, "_lab_processes", lambda: [])
    s = status.gather(tmp_path / "results" / "runs", now=NOW)
    assert sorted(s["flagged"]["B"]) == ["s2_long_shift__cpu__r1 [BAD]", "s3_cpu_stress__burn__r1 [INVALID]"]
    assert s["flagged"]["A"] == []                         # no runs in A: genuinely nothing flagged
    out = status.render(s)                                 # the screen names the runs, so nobody has to guess which ones to re-run
    assert "flagged: s2_long_shift__cpu__r1 [BAD]; s3_cpu_stress__burn__r1 [INVALID]" in out and " 2 flagged" in out


def test_a_failing_health_check_is_unknown_never_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "running_containers", lambda: None)
    monkeypatch.setattr(status, "_lab_processes", lambda: [])

    def boom(*a, **k):
        raise KeyError("flags")
    monkeypatch.setattr(gate, "health_rows", boom)
    runs = tmp_path / "results" / "runs"
    runs.mkdir(parents=True)
    s = status.gather(runs, now=NOW)
    assert s["flagged"] == {"B": None, "A": None}
    out = status.render(s)
    assert " ? flagged" in out and "could not be computed" in out


def test_unknown_flags_never_produce_the_all_clean_verdict():
    d = status.decide(_state(flagged={"B": [], "A": None}))
    assert "DONE" not in d["headline"] and "could not be computed" in d["headline"] and _cmds(d) == ["make health PHASE=A"]
    assert any("health flags of phase A could not be computed" in w for w in d["warnings"])


# ------------------------------------------------------------------------------------------------------------ v2.5.9: reproducible flags
def test_a_flag_that_survived_a_re_run_leads_to_accept_not_to_another_re_run():
    d = status.decide(_state(flagged={"B": ["a [SLOW]", "b [SLOW]"], "A": ["c [BAD]"]}, persistent={"B": ["a [SLOW]", "b [SLOW]"], "A": ["c [BAD]"]}))
    assert "STILL flagged" in d["headline"] and "will not change it" in d["headline"]
    assert _cmds(d) == ["make gate && { make accept-flagged PHASE=B; make accept-flagged PHASE=A; make status; }"]


def test_fresh_flags_are_re_run_first_and_reproducible_ones_are_listed_for_later():
    d = status.decide(_state(flagged={"B": ["a [SLOW]", "b [BAD]"], "A": []}, persistent={"B": ["a [SLOW]"], "A": []}))
    assert _cmds(d) == ["make gate && { make rerun-bad PHASE=B; make gate; make status; }"] and "1 run(s)" in d["headline"]
    assert any("still flagged after a re-run" in x and "accept-flagged PHASE=B" in x for x in d["after"])
    assert "make accept-flagged" in " ".join(t for k, t in d["steps"] if k == "say")      # the pre-v2.5.9 re-runs were not recorded


def test_accepted_runs_do_not_count_as_flagged_and_the_data_is_done(tmp_path, monkeypatch):
    _rerun_env(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "running_containers", lambda: None)
    monkeypatch.setattr(status, "_lab_processes", lambda: [])
    cli.main(["health", "--phase", "B", "--accept"])
    s = status.gather(tmp_path / "results" / "runs", now=NOW)
    assert s["accepted"]["B"] == 1 and len(s["flagged"]["B"]) == 1 and s["flagged"]["B"][0].startswith("s3_cpu_stress__burn__r1")   # INVALID stays flagged
    assert "kept after a re-run" in status.render(s)
