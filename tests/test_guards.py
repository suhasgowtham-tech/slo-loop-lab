"""Guards for the failures that were hit on a real Windows 11 + WSL2 + Docker Desktop machine.

Each test pins one failure mode so it cannot silently come back.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest
import yaml

from conftest import ROOT
from lab import cli, k8s
from lab.analysis import slo
from lab.analysis.detect import Detector, Rule, alert_intervals, auto_time_scale, evaluate, scale_detector, score

read = lambda p: (ROOT / p).read_text()  # noqa: E731


def test_missing_release_gives_an_instruction_not_a_helm_traceback(monkeypatch):
    def boom(*a, **k):
        raise k8s.CmdError("Error: release: not found")
    monkeypatch.setattr(k8s, "helm", boom)
    with pytest.raises(k8s.LabNotReady, match="make up"):
        k8s.ensure_release_deployed()


def test_cli_turns_not_ready_into_exit_code_2(monkeypatch, capsys):
    def boom(*a, **k):
        raise k8s.LabNotReady("run make up")
    monkeypatch.setattr("lab.capacity.probe_capacity", boom)
    assert cli.main(["capacity"]) == 2
    assert "run make up" in capsys.readouterr().err


def test_experiments_are_gated_on_a_fresh_verification():
    mk = read("Makefile")
    for target in ("latency", "capacity", "calibrate", "smoke", "phaseA", "phaseB"):
        assert re.search(rf"^{target}: require-verified", mk, re.M), target
    assert ".lab-verified" in mk
    # anything that changes the deployment must invalidate the marker; a green verify must create it
    for script in ("scripts/06-deploy-app.sh", "scripts/02-create-cluster.sh", "scripts/teardown.sh"):
        assert ".lab-verified" in read(script), script
    v = read("scripts/verify.sh")
    assert v.index('rm -f "$ROOT/.lab-verified"') < v.index("date -u") and "> \"$ROOT/.lab-verified\"" in v
    assert ".lab-verified" in read(".gitignore")


def test_verify_dry_runs_the_chart_inside_its_namespace():
    """Without -n the rendered NodePort Service collided in `default` and verify failed 6/6 for a healthy lab."""
    assert '| k -n "$NS_APP" apply --dry-run=server -f -' in read("scripts/verify.sh")


def test_image_build_survives_docker_desktop_attestations_and_creds_helper():
    b = read("scripts/05-build-images.sh")
    assert "--provenance=false" in b and "--sbom=false" in b and "--help" in b  # flags only when the builder supports them
    p = read("scripts/00-preflight.sh")
    assert "credsStore" in p and "docker-credential-" in p
    assert "Kubernetes" in p  # warns about Docker Desktop's built-in cluster
    assert "Get-NetTCPConnection" in p  # Windows-side port check (Docker Desktop publishes ports on Windows)


def test_image_tag_is_consistent_between_build_script_and_chart():
    tag = re.search(r'^TAG="([^"]+)"', read("scripts/05-build-images.sh"), re.M).group(1)
    imgs = yaml.safe_load(read("charts/slo-loop-app/values.yaml"))["images"]
    assert imgs["gateway"]["tag"] == tag == imgs["retriever"]["tag"]
    assert tag != "0.1.0", "behaviour of the retriever changed: a rebuilt image must not reuse the old tag"


def test_retriever_context_is_small_and_has_no_digit_tokens(apps_modules):
    r = apps_modules["retriever_app"]
    assert r.TOP_K == 1 and r.SNIPPET_WORDS <= 8
    assert not any(ch.isdigit() for d in r.DOCS for ch in d), "digits tokenise one by one and bloat every prompt"
    assert all(len(d.split()) == r.SNIPPET_WORDS for d in r.DOCS)


# ------------------------------------------------------------------ low-traffic alerting (time scaling)

THR = {"short": 2.0, "medium": 4.0, "long": 10.0}


def trickle(duration=600, rps=0.2, bad=lambda t: False) -> pd.DataFrame:
    n = int(duration * rps)
    rng = np.random.default_rng(3)
    rows = []
    for i in range(n):
        t = (i + 0.5) / rps
        cls = rng.choice(["short", "medium", "long"], p=[0.6, 0.3, 0.1])
        rows.append({"idx": i, "cls": cls, "t_sched": t, "t_send": t, "t_done": t + 0.5, "weight": 100, "status": "ok",
                     "ttft_s": THR[cls] * (1.5 if bad(t) else 0.2), "total_s": 1.0, "retrieve_s": 0.01, "llm_ttft_s": 0.2,
                     "prompt_tokens": 10, "completion_tokens": 5})
    return slo.annotate(pd.DataFrame(rows), THR)


GOOGLE = Detector("g", "request", (Rule(60, 5, 14.4), Rule(360, 30, 6)))


def test_scaling_keeps_each_rules_long_to_short_ratio_and_burn():
    s, dropped = scale_detector(GOOGLE, 3.0)
    assert [(r.long_s, r.short_s, r.burn) for r in s.rules] == [(180, 15, 14.4), (1080, 90, 6)] and not dropped


def test_rules_that_do_not_fit_in_the_run_are_dropped_not_truncated():
    s, dropped = scale_detector(GOOGLE, 3.0, max_long_s=480)
    assert len(s.rules) == 1 and len(dropped) == 1 and s.rules[0].long_s == 180


def test_auto_scale_is_1_at_high_rate_and_grows_at_low_rate_but_stays_inside_the_run():
    assert auto_time_scale(6000, 600, GOOGLE) == 1.0                # 10 req/s: nothing to fix
    mid = auto_time_scale(120, 600, GOOGLE)                          # 0.2 req/s
    assert mid > 1.0
    assert GOOGLE.rules[0].long_s * mid <= 0.8 * 600                   # the shortest long window must still fit in the run
    assert auto_time_scale(10, 600, GOOGLE) <= 6.0


def test_low_traffic_blinds_the_fast_rule_and_scaling_restores_it():
    """The core 'low-traffic alerting' problem. At 0.2 req/s a 5 s window holds ~1 event, so the FAST rule's min_events (5)
    is never met and it can never page. (The slow rule's 30 s window does hold ~6 events, so the full detector still
    catches the incident, only later: that is the cost the lab measures.)"""
    df = trickle(bad=lambda t: 200 <= t < 330)
    inc = [{"start": 200, "end": 330}]
    fast = Detector("fast", "request", (Rule(60, 5, 14.4),))
    assert not score(alert_intervals(*evaluate(df, fast, 0.99, 600)), inc, 600)["detected"]
    f = auto_time_scale(len(df), 600, fast)
    assert f > 1.0
    scaled, dropped = scale_detector(fast, f, 0.8 * 600)
    hit = score(alert_intervals(*evaluate(df, scaled, 0.99, 600)), inc, 600)
    assert hit["detected"] and not dropped, (f, hit)


def test_scaling_never_makes_the_full_detector_slower_to_detect_than_unscaled_on_low_traffic():
    df = trickle(bad=lambda t: 200 <= t < 330)
    inc = [{"start": 200, "end": 330}]
    plain = score(alert_intervals(*evaluate(df, GOOGLE, 0.99, 600)), inc, 600)
    f = auto_time_scale(len(df), 600, GOOGLE)
    scaled, _ = scale_detector(GOOGLE, f, 0.8 * 600)
    hit = score(alert_intervals(*evaluate(df, scaled, 0.99, 600)), inc, 600)
    assert hit["detected"]
    if plain["detected"]:
        assert hit["ttd_s"] <= plain["ttd_s"] + 30, (plain, hit)  # allow one short-window of slack: the stretch lowers time resolution


def test_scaling_does_not_create_false_pages_on_quiet_traffic():
    df = trickle(bad=lambda t: False)
    f = auto_time_scale(len(df), 600, GOOGLE)
    scaled, _ = scale_detector(GOOGLE, f, 0.8 * 600)
    res = score(alert_intervals(*evaluate(df, scaled, 0.99, 600)), [], 600)
    assert res["pages_total"] == 0


# ------------------------------------------------------------------ stale model mount after a Windows/WSL/Docker restart
# Real failure: after a restart the kind node came back with an EMPTY /models bind mount, the llm pod crash-looped with
# "failed to open GGUF file ... No such file or directory" and `make deploy` waited 8 minutes before failing.

import os
import shutil
import subprocess
import textwrap


@pytest.fixture()
def fake_host(tmp_path):
    """A repo copy whose docker/kubectl/helm are stubs: `docker exec ... test -s /models/..` succeeds only after
    `docker restart` was called (or always / never, via STUB_MODE)."""
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "scripts", root / "scripts")
    (root / "models").mkdir()
    (root / "models" / "m.gguf").write_bytes(b"GGUF" + b"0" * 64)
    (root / "charts").mkdir()
    (root / "config").mkdir()
    (root / "config" / "slo.yaml").write_text("slo: {}\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (stubs / "docker").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "docker $*" >> "{state}/calls"
        case "$1" in
          restart) touch "{state}/restarted" ;;
          exec) case "${{STUB_MODE:-after_restart}}" in
                  always) exit 0 ;;
                  never) exit 1 ;;
                  *) [ -f "{state}/restarted" ] || exit 1 ;;
                esac ;;
        esac
        exit 0
        """))
    for name in ("kubectl", "helm"):
        (stubs / name).write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{state}/calls"\nexit 0\n')
    for f in stubs.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "LAB_MODEL_FILE": "m.gguf", "HEAL_MOUNT_WAIT_S": "2",
           "HOME": str(tmp_path)}
    return root, env, state


def run_script(root, env, name, **extra):
    return subprocess.run(["bash", str(root / "scripts" / name)], env={**env, **extra}, capture_output=True, text=True, timeout=120)


def test_heal_restarts_the_node_when_the_model_mount_is_empty(fake_host):
    root, env, state = fake_host
    r = run_script(root, env, "heal.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (state / "calls").read_text()
    assert "docker restart slo-loop-control-plane" in calls
    assert "rollout restart deploy/llm deploy/gateway deploy/retriever" in calls  # no stale CrashLoopBackOff timer
    assert "make deploy && make verify" in r.stdout


def test_heal_does_nothing_destructive_when_already_healthy(fake_host):
    root, env, state = fake_host
    r = run_script(root, env, "heal.sh", STUB_MODE="always")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker restart" not in (state / "calls").read_text()


def test_heal_tells_you_to_recreate_the_cluster_when_a_restart_does_not_help(fake_host):
    root, env, _ = fake_host
    r = run_script(root, env, "heal.sh", STUB_MODE="never")
    assert r.returncode != 0
    assert "make down && make up" in r.stdout + r.stderr


def test_heal_refuses_when_the_model_is_missing_on_disk(fake_host):
    root, env, _ = fake_host
    (root / "models" / "m.gguf").unlink()
    r = run_script(root, env, "heal.sh")
    assert r.returncode != 0 and "make model" in r.stdout + r.stderr


def test_deploy_fails_in_seconds_with_the_fix_instead_of_waiting_eight_minutes(fake_host):
    root, env, state = fake_host
    r = run_script(root, env, "06-deploy-app.sh", STUB_MODE="never")
    assert r.returncode != 0 and "make heal" in r.stdout + r.stderr
    assert "helm upgrade" not in (state / "calls").read_text()


def test_verify_and_makefile_know_about_heal():
    assert "node_has_model" in read("scripts/verify.sh") and "node_has_model" in read("scripts/00-preflight.sh")
    assert re.search(r"^heal:", read("Makefile"), re.M) and "scripts/heal.sh" in read("Makefile")
    assert (ROOT / "scripts" / "heal.sh").stat().st_mode & 0o111, "heal.sh must be executable"


def test_diag_is_read_only_and_covers_the_things_that_explained_past_failures(fake_host):
    root, env, state = fake_host
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "load.yaml").write_text("load: {profile: lite}\n")
    r = run_script(root, env, "diag.sh", STUB_MODE="always")
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ("time / load", "running containers", "model visible inside the node", "nr_throttled", "config/slo.yaml", "Saved to",
                   "helm release history", "recent events", "logs deploy/llm"):
        assert needle in r.stdout, needle
    assert list((root / "results").glob("diag-*.txt"))
    calls = (state / "calls").read_text()
    assert not any(w in calls for w in ("docker restart", "docker stop", " delete ", "rollout restart", "helm upgrade"))
    assert re.search(r"^diag:", read("Makefile"), re.M)


def test_summarize_prints_replica_timeline_for_autoscaled_runs(tmp_path, capsys):
    import json
    import random
    from lab import loadgen
    d = tmp_path / "s1_burst__queue__r1"
    d.mkdir()
    rng = random.Random(2)
    rows = []
    for i in range(20):
        t = 3.0 + i * 8
        ttft = {"short": 0.7, "medium": 2.0, "long": 6.0}[c := rng.choice(["short", "medium", "long"])]
        rows.append({"idx": i, "cls": c, "t_sched": t, "t_send": t, "t_done": t + ttft + 1, "weight": 100, "status": "ok", "ttft_s": ttft,
                     "total_s": ttft + 1, "retrieve_s": 0.03, "llm_ttft_s": ttft - 0.03, "prompt_tokens": 60, "completion_tokens": 24})
    loadgen.write_rows(d / "requests.csv", rows)
    meta = {"phase": "X", "scenario": "s1_burst", "policy": "queue", "rep": 1, "duration_s": 200.0, "start_epoch": 1000.0, "end_epoch": 1200.0,
            "valid": True, "invalid_reasons": [], "max_lag_s": 0.01, "n_requests": 20, "slo": {"target": 0.99, "ttft_s": {"short": 2.0, "medium": 4.0, "long": 10.0}}}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "events.json").write_text(json.dumps({"incidents": [], "fault_events": []}))
    (d / "replicas.csv").write_text("epoch,spec,ready\n1000,1,1\n1005,1,1\n1100,2,1\n1110,2,2\n1190,1,2\n1200,1,1\n")
    assert cli.main(["summarize", str(d)]) == 0
    out = capsys.readouterr().out
    assert "llm replicas over the run" in out and "+0s 1/1" in out and "+100s 2/1" in out and "+110s 2/2" in out and "+200s 1/1" in out


# ---------------------------------------------------------------- v2.4: unattended phases survive one bad run
import types
from lab import config as cfg
from lab import runner


def _matrix_ns(only, reps=1):
    return types.SimpleNamespace(phase="A", reps=reps, only=only, dry_run=False)


def test_retriever_latency_scenario_is_refused_when_it_would_become_an_outage(monkeypatch):
    slo_ = {"ttft_s": {"short": 10.4, "medium": 15.6, "long": 16.3}}
    monkeypatch.setattr(k8s, "retrieve_timeout_s", lambda: 10.0)  # the old gateway default
    with pytest.raises(k8s.ScenarioNotRunnable, match="make deploy"):
        runner.check_scenario_runnable(cfg.load_scenario("s5_retriever_latency"), slo_)
    monkeypatch.setattr(k8s, "retrieve_timeout_s", lambda: 30.0)
    runner.check_scenario_runnable(cfg.load_scenario("s5_retriever_latency"), slo_)
    monkeypatch.setattr(k8s, "retrieve_timeout_s", lambda: 1 / 0)  # scenarios without that fault never ask the cluster
    runner.check_scenario_runnable(cfg.load_scenario("s1_burst"), slo_)
    assert issubclass(k8s.ScenarioNotRunnable, k8s.LabNotReady)


def test_chart_gives_the_gateway_a_retriever_timeout_above_the_s5_delay():
    vals = yaml.safe_load(read("charts/slo-loop-app/values.yaml"))
    assert vals["gateway"]["retrieveTimeoutS"] >= 30
    assert "RETRIEVE_TIMEOUT_S" in read("charts/slo-loop-app/templates/gateway.yaml")
    factor = cfg.load_scenario("s5_retriever_latency")["faults"][0]["params"]["latency_x_short_thr"]
    assert factor * 10.4 < 0.9 * vals["gateway"]["retrieveTimeoutS"]  # the real calibrated threshold on the maintainer's laptop


def _deploy_json(env):
    import json
    return json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "gateway", "env": env}]}}}})


def test_gateway_env_and_timeout_parsing(monkeypatch):
    seen = []

    def fake_kubectl(*args, **kw):
        seen.append(args)
        return _deploy_json([{"name": "REQUEST_TIMEOUT_S", "value": "60"}, {"name": "RETRIEVE_TIMEOUT_S", "value": "30"}])

    monkeypatch.setattr(k8s, "kubectl", fake_kubectl)
    assert k8s.gateway_env("RETRIEVE_TIMEOUT_S") == "30" and k8s.retrieve_timeout_s() == 30.0
    # regression: v2.4 passed a jsonpath template whose "\\n" Python turned into a REAL newline; real kubectl rejected it
    # ("unterminated quoted string") while a stubbed test could not notice. Plain `-o json`, and no argument may contain a newline.
    assert all(args[-2:] == ("-o", "json") for args in seen)
    assert not any("\n" in str(x) for args in seen for x in args)
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **k: _deploy_json([{"name": "A", "value": "1"}]))
    assert k8s.gateway_env("RETRIEVE_TIMEOUT_S") is None and k8s.retrieve_timeout_s() == 10.0
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **k: _deploy_json(None))  # a container with no env at all
    assert k8s.gateway_env("RETRIEVE_TIMEOUT_S") is None and k8s.retrieve_timeout_s() == 10.0
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **k: _deploy_json([{"name": "RETRIEVE_TIMEOUT_S", "value": "abc"}]))
    assert k8s.retrieve_timeout_s() == 10.0


def test_no_kubectl_or_helm_argument_in_the_package_contains_a_raw_newline():
    """Cheap static guard for the class of bug above: a Python '\\n' inside a normal (non-raw) string passed to kubectl/helm."""
    import ast
    for path in (cfg.ROOT / "lab").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("kubectl", "helm"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert "\n" not in arg.value, f"{path.name}:{node.lineno}: raw newline in a kubectl/helm argument {arg.value!r}"


def test_matrix_repairs_once_and_retries_a_failed_run(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "RESULTS_DIR", tmp_path)
    calls, repaired = [], []

    def run_one(phase, sc, pol, rep, **k):
        calls.append(sc)
        if len(calls) == 1:
            raise k8s.CmdError("helm upgrade timed out waiting for the condition")

    monkeypatch.setattr(runner, "run_one", run_one)
    monkeypatch.setattr(k8s, "recover", lambda *a, **k: repaired.append(1))
    assert cli._cmd_matrix(_matrix_ns(["s0_baseline"])) == 0
    assert calls == ["s0_baseline", "s0_baseline"] and repaired == [1]
    assert "attempt 1 failed" in (tmp_path / "matrix-failures.log").read_text()


def test_matrix_stops_after_two_jobs_in_a_row_fail_even_after_repair(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cfg, "RESULTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(runner, "run_one", lambda phase, sc, pol, rep, **k: (calls.append(sc), (_ for _ in ()).throw(k8s.CmdError("boom")))[1])
    monkeypatch.setattr(k8s, "recover", lambda *a, **k: None)
    assert cli._cmd_matrix(_matrix_ns(["s0_baseline", "s1_burst", "s2_long_shift"])) == 1
    assert len(calls) == 4, calls  # two jobs x (attempt + retry); the third job is never started
    err = capsys.readouterr().err
    assert "ABORTED" in err and "make recover" in err


def test_matrix_skips_a_scenario_that_cannot_work_and_keeps_going(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cfg, "RESULTS_DIR", tmp_path)
    calls, repaired = [], []

    def run_one(phase, sc, pol, rep, **k):
        calls.append(sc)
        if sc == "s5_retriever_latency":
            raise k8s.ScenarioNotRunnable("retriever delay 15.6 s would exceed the timeout")

    monkeypatch.setattr(runner, "run_one", run_one)
    monkeypatch.setattr(k8s, "recover", lambda *a, **k: repaired.append(1))
    assert cli._cmd_matrix(_matrix_ns(["s5_retriever_latency", "s0_baseline"])) == 1
    assert sorted(calls) == ["s0_baseline", "s5_retriever_latency"] and repaired == []  # no pointless repair/retry
    err = capsys.readouterr().err
    assert "SKIPPED" in err and "not runnable" in err


def test_matrix_does_not_retry_when_the_repair_itself_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "RESULTS_DIR", tmp_path)
    calls = []

    def run_one(phase, sc, pol, rep, **k):
        calls.append(sc)
        raise k8s.CmdError("boom")

    def bad_recover(*a, **k):
        raise k8s.LabNotReady("node did not become Ready")

    monkeypatch.setattr(runner, "run_one", run_one)
    monkeypatch.setattr(k8s, "recover", bad_recover)
    assert cli._cmd_matrix(_matrix_ns(["s0_baseline"])) == 1
    assert calls == ["s0_baseline"]
    assert "recovery failed" in (tmp_path / "matrix-failures.log").read_text()


def _recover_env(monkeypatch, status, heal_rc=0):
    seen = {"upgraded": 0}
    monkeypatch.setattr(k8s, "sh", lambda *a, **k: subprocess.CompletedProcess(a[0], heal_rc, stdout="healed\n", stderr=""))
    monkeypatch.setattr(k8s, "release_status", lambda: status)
    monkeypatch.setattr(k8s, "helm_set_policy", lambda p: seen.__setitem__("upgraded", seen["upgraded"] + 1))
    monkeypatch.setattr(k8s, "ensure_release_deployed", lambda: None)
    monkeypatch.setattr(k8s, "wait_ready", lambda *a, **k: None)
    monkeypatch.setattr(k8s, "autoscalers_present", lambda: [])
    monkeypatch.setattr(k8s, "settle_one_replica", lambda *a, **k: None)
    return seen


def test_recover_reapplies_a_failed_release(monkeypatch):
    seen = _recover_env(monkeypatch, "failed")
    k8s.recover(say=lambda m: None)
    assert seen["upgraded"] == 1


def test_recover_leaves_a_healthy_release_alone(monkeypatch):
    seen = _recover_env(monkeypatch, "deployed")
    k8s.recover(say=lambda m: None)
    assert seen["upgraded"] == 0


def test_recover_reports_what_a_human_must_do_for_stuck_or_missing_releases(monkeypatch):
    _recover_env(monkeypatch, "pending-upgrade")
    with pytest.raises(k8s.LabNotReady, match="rollback"):
        k8s.recover(say=lambda m: None)
    _recover_env(monkeypatch, None)
    with pytest.raises(k8s.LabNotReady, match="make up"):
        k8s.recover(say=lambda m: None)


def test_recover_gives_a_clear_error_when_the_upgrade_itself_fails(monkeypatch):
    _recover_env(monkeypatch, "failed")

    def boom(p):
        raise k8s.CmdError("timed out")

    monkeypatch.setattr(k8s, "helm_set_policy", boom)
    with pytest.raises(k8s.LabNotReady, match="make diag"):
        k8s.recover(say=lambda m: None)


def test_recover_is_a_make_target_and_cli_command(monkeypatch, capsys):
    assert re.search(r"^recover:", read("Makefile"), re.M) and "$(LAB) recover" in read("Makefile")
    called = []
    monkeypatch.setattr(k8s, "recover", lambda *a, **k: called.append(1))
    assert cli.main(["recover"]) == 0 and called == [1]
    assert "make verify" in capsys.readouterr().out


def test_summarize_explains_why_attribution_abstained(tmp_path, capsys):
    import json
    from lab import loadgen
    d = tmp_path / "s5_retriever_latency__static1__r1"
    d.mkdir()
    rows = []
    for i, t in enumerate(range(20, 52, 4)):  # healthy baseline
        rows.append({"idx": i, "cls": "short", "t_sched": t, "t_send": t, "t_done": t + 1.0, "weight": 100, "status": "ok", "ttft_s": 0.6,
                     "total_s": 1.5, "retrieve_s": 0.03, "llm_ttft_s": 0.57, "prompt_tokens": 60, "completion_tokens": 24})
    for j, t in enumerate(range(60, 120, 4)):  # every request in the incident hits the retriever timeout
        rows.append({"idx": 100 + j, "cls": "short", "t_sched": t, "t_send": t, "t_done": t + 10.0, "weight": 100, "status": "retriever_error",
                     "ttft_s": None, "total_s": 10.0, "retrieve_s": 10.0, "llm_ttft_s": None, "prompt_tokens": 0, "completion_tokens": 0})
    loadgen.write_rows(d / "requests.csv", rows)
    meta = {"phase": "A", "scenario": "s5_retriever_latency", "policy": "static1", "rep": 1, "duration_s": 200.0, "start_epoch": 1000.0,
            "end_epoch": 1200.0, "valid": True, "invalid_reasons": [], "max_lag_s": 0.01, "n_requests": len(rows),
            "slo": {"target": 0.99, "ttft_s": {"short": 10.4, "medium": 15.6, "long": 16.3}}}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "events.json").write_text(json.dumps({"incidents": [{"start": 60, "end": 120, "root_cause": "retriever"}], "fault_events": []}))
    assert cli.main(["summarize", str(d)]) == 0
    out = capsys.readouterr().out
    assert "root-cause attribution: truth=retriever" in out and "predicted=none (abstained)" in out and "15 failed" in out
    assert "fault effect" in out and "100% of the 15 requests sent in the window were BAD, versus 0% of the 8" in out
    assert "WARNING: no visible effect" not in out


def test_summarize_warns_when_a_fault_had_no_visible_effect(tmp_path, capsys):
    import json
    from lab import loadgen
    d = tmp_path / "s6_netem_delay__static1__r1"
    d.mkdir()
    rows = [{"idx": i, "cls": "short", "t_sched": 20 + 4 * i, "t_send": 20 + 4 * i, "t_done": 21 + 4 * i, "weight": 100, "status": "ok", "ttft_s": 0.6,
             "total_s": 1.5, "retrieve_s": 0.03, "llm_ttft_s": 0.57, "prompt_tokens": 60, "completion_tokens": 24} for i in range(40)]
    loadgen.write_rows(d / "requests.csv", rows)
    meta = {"phase": "A", "scenario": "s6_netem_delay", "policy": "static1", "rep": 1, "duration_s": 200.0, "start_epoch": 1000.0,
            "end_epoch": 1200.0, "valid": True, "invalid_reasons": [], "max_lag_s": 0.01, "n_requests": len(rows),
            "slo": {"target": 0.99, "ttft_s": {"short": 10.4, "medium": 15.6, "long": 16.3}}}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "events.json").write_text(json.dumps({"incidents": [{"start": 100, "end": 160, "kind": "netem_delay", "root_cause": "llm"}], "fault_events": []}))
    assert cli.main(["summarize", str(d)]) == 0
    out = capsys.readouterr().out
    assert "fault effect (netem_delay" in out and "0% of the 15 requests" in out and "WARNING: no visible effect" in out


def test_a_request_that_crashes_unexpectedly_is_recorded_not_lost(monkeypatch):
    """The load generator used to catch only httpx errors; any other exception made the row vanish (a failed request
    silently disappearing from the data would understate the error rate exactly when the system is broken)."""
    import asyncio
    import time as _t
    from lab import loadgen

    class Boom:
        def stream(self, *a, **k):
            raise OSError("connection reset by peer")

    row = asyncio.run(loadgen._one(Boom(), loadgen.Arrival(3, 0.0, "short"), {"prompt": "hello", "max_tokens": 4}, _t.perf_counter(), 5.0))
    assert row["status"] == "client_error" and row["idx"] == 3 and row["total_s"] is not None

    async def crash(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(loadgen, "_one", crash)  # even if _one itself ever crashes, run_load keeps the row
    arrivals = [loadgen.Arrival(i, 0.01 * i, "short") for i in range(5)]
    bodies = [{"prompt": "hello world", "max_tokens": 4} for _ in arrivals]
    res = asyncio.run(loadgen.run_load("http://127.0.0.1:9", arrivals, bodies, request_timeout_s=2.0))
    assert len(res.rows) == res.n_scheduled == 5
    assert {r["status"] for r in res.rows} == {"client_error"} and [r["idx"] for r in res.rows] == [0, 1, 2, 3, 4]
