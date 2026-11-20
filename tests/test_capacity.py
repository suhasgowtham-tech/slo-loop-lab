"""The capacity probe must adapt to the machine instead of aborting (regression: `make capacity` died at the first
step on a laptop where the default workload costs ~8 s per request).

These tests run the REAL probe + load generator + gateway against a fake llama.cpp with ONE slot and a known
service time (~0.3 s => capacity ~3 req/s). Kubernetes calls are stubbed; everything else is real.
"""
from __future__ import annotations

import shutil

import pandas as pd
import pytest
import yaml

from conftest import ROOT, Stack
from lab import capacity, k8s
from lab import config as cfg


@pytest.fixture(scope="module")
def slow_stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("capstack"), llm_env={
        "FAKE_SLOTS": "1", "FAKE_FIRST_TOKEN_S": "0.25", "FAKE_TOKEN_S": "0.0", "FAKE_PREFILL_S_PER_1K_CHARS": "0.05"})
    yield s
    s.stop()


@pytest.fixture()
def lab(tmp_path, monkeypatch, slow_stack):
    conf = tmp_path / "config"
    shutil.copytree(ROOT / "config", conf)
    monkeypatch.setattr(cfg, "CONFIG_DIR", conf)
    monkeypatch.setattr(cfg, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(cfg, "GATEWAY_URL", slow_stack.gateway.url)
    monkeypatch.setattr(k8s, "ensure_release_deployed", lambda: None)
    monkeypatch.setattr(k8s, "set_policy", lambda p: None)
    monkeypatch.setattr(k8s, "helm_upgrade_full", lambda extra=None: None)
    return tmp_path


def test_latency_measures_every_class_and_estimates_capacity(lab):
    out = capacity.measure_latency(per_class=2, warmup=1)
    assert set(out["per_class"]) == {"short", "medium", "long"}
    assert 0.2 < out["mean_service_s"] < 1.5
    assert out["est_capacity_rps"] == pytest.approx(1 / out["mean_service_s"], rel=0.01)
    saved = cfg.load_load()["latency_idle"]
    assert saved["mean_service_s"] == out["mean_service_s"]


def test_probe_finds_saturation_near_the_true_capacity(lab):
    res = capacity.probe_capacity(step_s=3, factor=1.6, min_requests=6, pause_s=0.2, drain_s=0.2)
    assert res["saturated"], res["steps"]
    assert 0.6 <= res["capacity_1replica_rps"] <= 4.5, res["steps"]  # true ~3 rps; loose on purpose (few samples per step)
    assert res["base_rps"] == pytest.approx(round(capacity.BASE_FRACTION * res["capacity_1replica_rps"], 3))
    assert cfg.load_load()["base_fraction"] == capacity.BASE_FRACTION
    load = cfg.load_load()
    assert load["calibrated"] is True and load["base_rps"] == res["base_rps"]
    assert list((cfg.RESULTS_DIR / "capacity").glob("probe-*.json")), "probe data must be saved for the write-up"


def test_probe_retreats_instead_of_aborting_when_the_first_step_is_too_heavy(lab, monkeypatch):
    """The user's failure: the first offered rate was above capacity, and the old probe could only climb."""
    real = capacity.measure_latency
    def optimistic(**kw):
        out = real(per_class=2, warmup=1)
        out["est_capacity_rps"] = 30.0  # pretend the idle estimate was 10x too optimistic => start at 10.5 rps
        return out
    monkeypatch.setattr(capacity, "measure_latency", optimistic)
    res = capacity.probe_capacity(step_s=3, factor=1.6, min_requests=6, pause_s=0.2, drain_s=0.2)
    assert res["retreats"] >= 1, res["steps"]
    assert res["steps"][0]["passed"] is False
    assert res["capacity_1replica_rps"] > 0


def test_probe_gives_actionable_error_after_all_retreats_fail(lab, monkeypatch):
    monkeypatch.setattr(capacity, "measure_latency", lambda **kw: {
        "est_capacity_rps": 1.0, "idle_ttft_p95_s": 1.0, "per_class": {}, "mean_service_s": 1.0})
    dead = pd.DataFrame({"status": ["client_timeout"] * 10, "ttft_s": [None] * 10, "total_s": [90.0] * 10, "cls": ["short"] * 10})
    monkeypatch.setattr(capacity, "_step", lambda *a, **k: dead)
    with pytest.raises(RuntimeError, match="Best performance"):
        capacity.probe_capacity(step_s=1, pause_s=0, drain_s=0)


def test_an_overloaded_first_step_is_not_its_own_reference(lab, monkeypatch):
    """Bug found while writing this suite: using step 1 as the TTFT yardstick let an overloaded step 1 'pass'."""
    monkeypatch.setattr(capacity, "measure_latency", lambda **kw: {
        "est_capacity_rps": 1.0, "idle_ttft_p95_s": 1.0, "per_class": {}, "mean_service_s": 1.0})
    slow = pd.DataFrame({"status": ["ok"] * 30, "ttft_s": [30.0] * 30, "total_s": [31.0] * 30, "cls": ["short"] * 30})
    monkeypatch.setattr(capacity, "_step", lambda *a, **k: slow)
    with pytest.raises(RuntimeError):
        capacity.probe_capacity(step_s=1, pause_s=0, drain_s=0)


@pytest.mark.parametrize("rps,expected", [(0.17, 1259), (0.5, 600), (0.05, 1500)])
def test_calibration_duration_collects_enough_rare_class_samples(rps, expected):
    assert capacity.auto_calibration_seconds(rps, {"short": 0.6, "medium": 0.3, "long": 0.1}) == expected


def test_calibration_duration_is_not_a_coin_flip():
    """Regression found by review: an EXPECTED rare-class count equal to the minimum fails the minimum ~50 % of the time."""
    import math
    mix = {"short": 0.6, "medium": 0.3, "long": 0.1}
    for rps in (0.12, 0.15, 0.2):
        mean = capacity.auto_calibration_seconds(rps, mix) * rps * 0.1
        p_ok = 1.0 - sum(math.exp(-mean) * mean ** k / math.factorial(k) for k in range(15))  # P(Poisson(mean) >= 15)
        assert p_ok >= 0.9 or capacity.auto_calibration_seconds(rps, mix) == 1500, (rps, mean, p_ok)


class _Res:
    def __init__(self, rows):
        self.rows = rows


def _calib_rows(n_short, n_medium, n_long):
    rows = []
    for cls, n, t in (("short", n_short, 0.7), ("medium", n_medium, 2.0), ("long", n_long, 5.0)):
        rows += [{"cls": cls, "status": "ok", "ttft_s": t + 0.01 * i, "total_s": t + 1} for i in range(n)]
    return rows


@pytest.fixture()
def calib(lab, monkeypatch):
    from lab import runner
    monkeypatch.setenv("LAB_QUIET_WAIT_S", "0")
    monkeypatch.setattr(runner, "warmup", lambda *a, **k: None)
    cfg.write_load_config({"calibrated": True, "base_rps": 0.15})
    def feed(rows):
        monkeypatch.setattr(runner, "_run_load_sync", lambda *a, **k: _Res(rows))
    return feed


def test_calibration_accepts_a_slightly_short_rare_class_with_a_warning(calib, capsys):
    calib(_calib_rows(60, 30, 11))  # 11 'long' samples < 15 target but >= 8 hard minimum: proceed, conservatively
    out = capacity.calibrate_slo(duration_s=600)
    assert "WARNING: only 11 'long' samples" in capsys.readouterr().out
    thr = cfg.load_slo()["ttft_s"]
    assert thr["short"] < thr["medium"] < thr["long"] and cfg.load_slo()["calibrated"] is True and out["slo"]["calibrated"]


def test_calibration_refuses_when_a_class_is_hopelessly_under_sampled(calib):
    calib(_calib_rows(60, 30, 5))
    with pytest.raises(RuntimeError, match="at least 8"):
        capacity.calibrate_slo(duration_s=600)


def test_profile_switch_resets_everything_that_depends_on_it(lab):
    cfg.write_load_config({"calibrated": True, "capacity_1replica_rps": 0.3, "base_rps": 0.15, "capacity_probe": [{"x": 1}], "latency_idle": {"a": 1}})
    (cfg.CONFIG_DIR / "slo.yaml").write_text(yaml.safe_dump({"slo": {"calibrated": True, "target": 0.99, "request_timeout_s": 60,
                                                                    "ttft_s": {"short": 1, "medium": 2, "long": 3}, "calibration": {"x": 1}}}))
    cfg.apply_profile("standard")
    load = cfg.load_load()
    assert load["profile"] == "standard" and load["calibrated"] is False and load["capacity_1replica_rps"] is None
    assert load["classes"]["long"]["words"] == 900
    assert "capacity_probe" not in load and "latency_idle" not in load
    assert cfg.load_slo()["calibrated"] is False and "calibration" not in cfg.load_slo()
    with pytest.raises(KeyError):
        cfg.apply_profile("nope")


def test_shipped_default_is_the_lite_profile_and_matches_profiles_yaml():
    load = yaml.safe_load((ROOT / "config" / "load.yaml").read_text())["load"]
    prof = yaml.safe_load((ROOT / "config" / "profiles.yaml").read_text())["profiles"]
    assert load["profile"] in prof and load["classes"] == prof[load["profile"]]["classes"]
    if not load.get("calibrated"):
        # an untouched checkout ships the lite profile; once `make calibrate` has run the file holds the user's own
        # measurements (calibrated: true), which is correct and must not fail the suite (it did in v2.5: 1 failed on a calibrated machine)
        assert load["profile"] == "lite"
    # lite must keep the SHAPE of the problem: three classes with a strictly increasing cost
    w = [prof["lite"]["classes"][c]["words"] for c in ("short", "medium", "long")]
    assert w[0] < w[1] < w[2] and w[2] / w[0] >= 8


# ------------------------------------------------------------------ bounded wait for a quiet machine

def test_wait_until_quiet_returns_immediately_when_quiet(monkeypatch, capsys):
    monkeypatch.setattr(capacity, "_load1", lambda: 0.3)
    assert capacity.wait_until_quiet(max_wait_s=5, poll_s=0.01) == 0.3
    assert "quiet" in capsys.readouterr().out


def test_wait_until_quiet_waits_for_the_load_to_fall(monkeypatch, capsys):
    seq = iter([4.0, 3.0, 0.5])
    monkeypatch.setattr(capacity, "_load1", lambda: next(seq))
    assert capacity.wait_until_quiet(max_wait_s=5, poll_s=0.01) == 0.5
    assert "waiting" in capsys.readouterr().out


def test_wait_until_quiet_is_bounded_and_never_fatal(monkeypatch, capsys):
    monkeypatch.setattr(capacity, "_load1", lambda: 9.0)
    assert capacity.wait_until_quiet(max_wait_s=0.05, poll_s=0.01) == 9.0
    assert "WARNING" in capsys.readouterr().out


def test_quiet_limit_scales_with_cpus_but_never_below_one(monkeypatch):
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 4)
    assert capacity.quiet_limit() == 1.0
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 16)
    assert capacity.quiet_limit() == pytest.approx(2.4)


def test_calibration_saves_its_baseline_and_explains_unordered_thresholds(calib, capsys):
    rows = _calib_rows(60, 30, 20)
    for r in rows:  # make 'medium' slower than 'long': queueing, not prompt cost, dominates baseline TTFT
        if r["cls"] == "medium":
            r["ttft_s"] += 9.0
    calib(rows)
    capacity.calibrate_slo(duration_s=600)
    out = capsys.readouterr().out
    assert "baseline saved to" in out and "not ordered short <= medium <= long" in out
    saved = list((cfg.RESULTS_DIR / "calibration").glob("baseline-*.csv"))
    assert len(saved) == 1 and len(pd.read_csv(saved[0])) == len(rows)
    per = cfg.load_slo()["calibration"]["per_class"]
    assert {"n_ok", "p99", "p50"} <= set(per["long"])


def test_base_fraction_keeps_the_baseline_below_the_knee_and_the_burst_above_throughput():
    """On the reference laptop the latency knee was 0.178 rps while one replica's idle-latency throughput was 0.33 rps:
    base = 0.5 x knee made the 3x 'burst' only 81 % of throughput. 0.75 x knee -> 121 %: a true overload with ~50 % more events."""
    knee, throughput = 0.178, 0.33
    assert capacity.BASE_FRACTION < 1.0                       # baseline below the knee
    assert 3 * capacity.BASE_FRACTION * knee > throughput     # burst genuinely exceeds what one replica can serve
    assert 3 * 0.5 * knee < throughput                        # ...which 0.5 did not


def test_set_base_rps_changes_the_load_marks_thresholds_stale_and_validates(lab, capsys):
    cfg.write_load_config({"calibrated": True, "capacity_1replica_rps": 0.2, "base_rps": 0.1,
                           "latency_idle": {"est_capacity_rps": 0.33}})
    (cfg.CONFIG_DIR / "slo.yaml").write_text(yaml.safe_dump({"slo": {"calibrated": True, "target": 0.99, "request_timeout_s": 60,
                                                                    "ttft_s": {"short": 1, "medium": 2, "long": 3}, "calibration": {"x": 1}}}))
    out = capacity.set_base_rps(0.9)
    assert out["base_rps"] == 0.18 and cfg.load_load()["base_rps"] == 0.18 and cfg.load_load()["base_fraction"] == 0.9
    assert cfg.load_slo()["calibrated"] is False and "make calibrate" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="between 0.2 and 1.0"):
        capacity.set_base_rps(1.4)
    cfg.write_load_config({"calibrated": False})
    with pytest.raises(RuntimeError, match="make capacity"):
        capacity.set_base_rps(0.7)


def test_cli_exposes_set_base_and_the_makefile_target(lab, capsys):
    from lab import cli
    cfg.write_load_config({"calibrated": True, "capacity_1replica_rps": 0.2, "base_rps": 0.1})
    assert cli.main(["set-base", "--frac", "0.5"]) == 0
    assert cfg.load_load()["base_rps"] == 0.1
    assert "set-base" in (ROOT / "Makefile").read_text()
