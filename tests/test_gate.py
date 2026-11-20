"""v2.5.2: the health gate (`make gate`), the 'Docker is down is not a lost cluster' fix, and the stray-container warning.
Offline; no cluster. Real incidents behind these tests (10 Nov 2026): the same lab scored 100 % good at night and 89 % good a few hours
later (a 12-hour run would have measured the laptop), `make heal` said 'the cluster is gone' while Docker Desktop was merely stopped, and
two old kind clusters woke up after a Docker restart and ate CPU."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap

from conftest import ROOT
from lab import cli, gate
from lab import config as cfg
from lab import runner
from test_v25 import make_run

read = lambda p: (ROOT / p).read_text()  # noqa: E731


def healthy(t):  # 2 bad of 120 requests (1.7 %)
    return int(t / 5) % 100 == 0


def sick(t):  # 1 bad in 9 (11 %)
    return int(t / 5) % 9 == 0


def _run(tmp_path, bad, name="s0_baseline__static1__r1"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return make_run(tmp_path, name=name, incident=None, bad=bad, rps=0.2, duration=600)


# ------------------------------------------------------------------------------------------- verdict
def test_a_run_with_at_least_97_percent_good_passes(tmp_path):
    v = gate.evaluate(_run(tmp_path, healthy), 97.0)
    assert v["passed"] and v["n"] == 120 and 98.0 < v["good_pct"] < 99.0 and v["reasons"] == []
    txt = gate.format_verdict(v, "G/x", 97.0)
    assert txt.startswith("GATE PASS") and "healthy" in txt


def test_a_run_below_the_gate_fails_and_says_not_to_start_a_long_run(tmp_path):
    v = gate.evaluate(_run(tmp_path, sick), 97.0)
    assert not v["passed"] and v["good_pct"] < 97.0 and "below the 97% gate" in v["reasons"][0]
    txt = gate.format_verdict(v, "G/x", 97.0)
    assert txt.startswith("GATE FAIL") and "Do NOT start a long run" in txt and "make gate" in txt


def test_an_invalid_run_never_passes_even_with_perfect_requests(tmp_path):
    d = _run(tmp_path, lambda t: False)
    meta = json.loads((d / "meta.json").read_text())
    meta.update(valid=False, invalid_reasons=["load generator lag 3.2 s"])
    (d / "meta.json").write_text(json.dumps(meta))
    v = gate.evaluate(d, 97.0)
    assert not v["passed"] and "invalid" in v["reasons"][0] and "lag" in v["reasons"][0]


def test_next_rep_counts_up_and_ignores_other_phases(tmp_path):
    runs = tmp_path / "runs"
    assert gate.next_rep(runs) == 1
    for p in ("G/s0_baseline__static1__r1", "G/s0_baseline__static1__r4", "X/s0_baseline__static1__r9", "G/notes"):
        (runs / p).mkdir(parents=True)
    assert gate.next_rep(runs) == 5


def test_stray_containers_are_everything_but_the_lab_node_and_unreachable_docker_is_none():
    run = ["slo-loop-control-plane", "lab-control-plane", "kind-control-plane"]
    assert gate.stray_containers(run, "slo-loop-control-plane") == ["kind-control-plane", "lab-control-plane"]
    assert gate.stray_containers(["slo-loop-control-plane"], "slo-loop-control-plane") == []
    assert gate.stray_containers(None, "slo-loop-control-plane") == []


# ------------------------------------------------------------------------------------------- the command
def _wire(tmp_path, monkeypatch, bad, containers=("slo-loop-control-plane",)):
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    calls = []

    def fake_run_one(phase, scenario, policy, rep, force=False, **kw):
        calls.append((phase, scenario, policy, rep, force))
        d = runs / phase
        d.mkdir(exist_ok=True)
        return make_run(d, name=f"{scenario}__{policy}__r{rep}", incident=None, bad=bad, rps=0.2, duration=600)

    monkeypatch.setattr(runner, "run_one", fake_run_one)
    monkeypatch.setattr(gate, "running_containers", lambda: None if containers is None else list(containers))
    return calls


def test_gate_command_runs_one_static1_baseline_in_phase_G_with_a_fresh_rep_each_time(tmp_path, monkeypatch, capsys):
    calls = _wire(tmp_path, monkeypatch, healthy)
    assert cli.main(["gate"]) == 0
    assert "GATE PASS" in capsys.readouterr().out
    assert cli.main(["gate"]) == 0
    assert calls == [("G", "s0_baseline", "static1", 1, True), ("G", "s0_baseline", "static1", 2, True)]


def test_gate_command_exits_1_when_the_machine_is_sick_so_a_chained_night_does_not_start(tmp_path, monkeypatch, capsys):
    _wire(tmp_path, monkeypatch, sick)
    assert cli.main(["gate"]) == 1
    out = capsys.readouterr().out
    assert "GATE FAIL" in out and "Do NOT start a long run" in out


def test_gate_min_good_is_adjustable(tmp_path, monkeypatch, capsys):
    _wire(tmp_path, monkeypatch, sick)                     # ~88.9 % good
    assert cli.main(["gate", "--min-good", "85"]) == 0
    assert "GATE PASS" in capsys.readouterr().out


def test_gate_warns_with_ready_to_paste_commands_about_other_running_containers(tmp_path, monkeypatch, capsys):
    _wire(tmp_path, monkeypatch, healthy, containers=("slo-loop-control-plane", "lab-control-plane", "kind-control-plane"))
    assert cli.main(["gate"]) == 0
    out = capsys.readouterr().out
    assert "docker stop kind-control-plane lab-control-plane" in out
    assert "docker update --restart=no kind-control-plane lab-control-plane" in out
    assert "<" not in "".join(ln for ln in out.splitlines() if ln.startswith("[gate] Stop") or ln.startswith("[gate] and"))


def test_gate_refuses_with_a_clear_message_when_docker_is_not_reachable(tmp_path, monkeypatch, capsys):
    calls = _wire(tmp_path, monkeypatch, healthy, containers=None)
    assert cli.main(["gate"]) == 1
    err = capsys.readouterr().err
    assert "Docker is not reachable" in err and "not a lost cluster" in err and calls == []


def test_gate_history_lists_every_gate_with_a_verdict(tmp_path, monkeypatch, capsys):
    _wire(tmp_path, monkeypatch, healthy)
    cli.main(["gate"])
    _wire(tmp_path, monkeypatch, sick)                     # re-wires fake_run_one; the G directory (r1) is kept
    cli.main(["gate"])
    capsys.readouterr()
    assert cli.main(["gate", "--history"]) == 0
    out = capsys.readouterr().out
    assert "s0_baseline__static1__r1" in out and "s0_baseline__static1__r2" in out
    assert re.search(r"r1 .*pass", out) and re.search(r"r2 .*FAIL", out)


def test_gate_history_with_no_gate_runs_says_how_to_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "RUNS_DIR", tmp_path / "runs")
    assert cli.main(["gate", "--history"]) == 0
    assert "make gate" in capsys.readouterr().out


def test_make_target_and_docs_exist():
    mk = read("Makefile")
    assert re.search(r"^gate: require-verified", mk, re.M) and "$(LAB) gate" in mk and " gate " in mk.split("\n")[7]
    assert "make gate" in read("README.md") and "tmux new -s lab" in read("docs/TROUBLESHOOTING.md")
    assert "Ctrl+b then d" in read("docs/TROUBLESHOOTING.md") and "tmux attach -t lab" in read("docs/TROUBLESHOOTING.md")


# ------------------------------------------------------------------------------------------- Docker down is not a lost cluster
def _fake_repo(tmp_path, docker_script):
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "scripts", root / "scripts")
    (root / "models").mkdir()
    (root / "models" / "m.gguf").write_bytes(b"GGUF" + b"0" * 64)
    (root / "config").mkdir()
    (root / "config" / "slo.yaml").write_text("slo: {}\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "docker").write_text(textwrap.dedent(docker_script))
    for name in ("kubectl", "helm"):
        (stubs / name).write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in stubs.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "LAB_MODEL_FILE": "m.gguf", "HEAL_MOUNT_WAIT_S": "2", "HOME": str(tmp_path)}
    return root, env


def _heal(root, env):
    return subprocess.run(["bash", str(root / "scripts" / "heal.sh")], env=env, capture_output=True, text=True, timeout=60)


def test_heal_says_docker_is_not_reachable_instead_of_claiming_the_cluster_is_gone(tmp_path):
    root, env = _fake_repo(tmp_path, """\
        #!/usr/bin/env bash
        echo "The command 'docker' could not be found in this WSL 2 distro." >&2
        exit 1
        """)
    r = _heal(root, env)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "Docker is not reachable" in out and "NOT a lost cluster" in out and "Engine running" in out
    assert "the cluster is gone" not in out


def test_heal_still_reports_a_missing_node_when_docker_itself_works(tmp_path):
    root, env = _fake_repo(tmp_path, """\
        #!/usr/bin/env bash
        case "$1" in
          info) exit 0 ;;
          inspect) exit 1 ;;
        esac
        exit 0
        """)
    r = _heal(root, env)
    assert r.returncode != 0 and "the cluster is gone" in r.stdout + r.stderr and "make up" in r.stdout + r.stderr


def test_run_start_error_points_at_docker_before_make_up():
    k = read("lab/k8s.py")
    body = k[k.index("def ensure_release_deployed"):k.index("def gateway_env")]
    msg = body[body.index("raise LabNotReady"):]
    assert msg.index("docker ps") < msg.index("make up") and "NOT lost" in msg


def test_diag_warns_about_stray_containers_with_paste_ready_commands():
    d = read("scripts/diag.sh")
    assert "share your CPU" in d and "docker stop $others" in d and "docker update --restart=no $others" in d


# ------------------------------------------------------------------------------------------- v2.5.2: cpu -> static race
# Real incident (10 Nov 19:45): after a cpu-policy run, `static1` left llm at 2 replicas (the KEDA-owned HPA is garbage-collected
# asynchronously and scaled the Deployment back up), so wait_ready('llm', 1) timed out with spec/ready=(2, 2).
from lab import k8s  # noqa: E402


def _fake_cluster(monkeypatch, autoscaler_polls, spec):
    """autoscaler_polls: successive answers of autoscalers_present(); spec: replicas the Deployment currently has."""
    log = {"scaled": [], "sleeps": 0, "polls": list(autoscaler_polls)}
    monkeypatch.setattr(k8s, "autoscalers_present", lambda: log["polls"].pop(0) if len(log["polls"]) > 1 else log["polls"][0])
    monkeypatch.setattr(k8s, "deployment_replicas", lambda name="llm": (spec, spec))
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **k: log["scaled"].append(a) or "")
    monkeypatch.setattr(k8s.time, "sleep", lambda s: log.__setitem__("sleeps", log["sleeps"] + 1))
    return log


def test_settle_waits_for_the_autoscaler_to_disappear_then_forces_one_replica(monkeypatch):
    log = _fake_cluster(monkeypatch, [["hpa/keda-hpa-llm", "scaledobject/llm"], ["hpa/keda-hpa-llm"], []], spec=2)
    k8s.settle_one_replica(timeout_s=60, poll_s=1)
    assert log["sleeps"] == 2
    assert log["scaled"] == [("-n", k8s.NAMESPACE, "scale", "deploy/llm", "--replicas=1")]


def test_settle_does_not_touch_a_deployment_that_is_already_one_replica(monkeypatch):
    log = _fake_cluster(monkeypatch, [[]], spec=1)
    k8s.settle_one_replica()
    assert log["scaled"] == [] and log["sleeps"] == 0


def test_settle_gives_up_waiting_after_the_timeout_but_still_sets_the_replica_count(monkeypatch):
    log = _fake_cluster(monkeypatch, [["hpa/stuck"]], spec=2)
    k8s.settle_one_replica(timeout_s=0, poll_s=1)
    assert log["sleeps"] == 0 and len(log["scaled"]) == 1


def test_set_policy_settles_after_static1_and_before_waiting_for_one_replica(monkeypatch):
    order = []
    monkeypatch.setattr(k8s, "ensure_release_deployed", lambda: order.append("ensure"))
    monkeypatch.setattr(k8s, "helm_set_policy", lambda p: order.append(f"helm:{p}"))
    monkeypatch.setattr(k8s, "settle_one_replica", lambda *a, **k: order.append("settle"))
    monkeypatch.setattr(k8s, "wait_ready", lambda name, want, *a, **k: order.append(f"wait:{want}"))
    monkeypatch.setattr(k8s, "restart_all", lambda *a, **k: order.append("restart"))
    k8s.set_policy("static3")
    assert order == ["ensure", "helm:static1", "settle", "wait:1", "restart", "wait:1", "helm:static3", "wait:3"]


def test_recover_removes_a_lingering_autoscaler_before_settling(monkeypatch):
    order = []
    monkeypatch.setattr(k8s, "sh", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="healed\n", stderr=""))
    monkeypatch.setattr(k8s, "release_status", lambda: "deployed")
    monkeypatch.setattr(k8s, "ensure_release_deployed", lambda: None)
    monkeypatch.setattr(k8s, "autoscalers_present", lambda: ["hpa/keda-hpa-llm"])
    monkeypatch.setattr(k8s, "helm_set_policy", lambda p: order.append(f"helm:{p}"))
    monkeypatch.setattr(k8s, "settle_one_replica", lambda *a, **k: order.append("settle"))
    monkeypatch.setattr(k8s, "wait_ready", lambda name, want, *a, **k: order.append(f"wait:{want}"))
    k8s.recover(say=lambda m: None)
    assert order == ["helm:static1", "settle", "wait:1"]
