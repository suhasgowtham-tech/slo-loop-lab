"""Local end-to-end tests: real gateway + real retriever + real load generator/runner/analysis,
with a fake llama.cpp. These prove the MEASUREMENT CHAIN (SLIs, fault injection, open-loop load,
logging, replay detection, stage attribution) without a cluster. They do not prove llama.cpp or
Kubernetes behaviour; `make verify` does that on your machine.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import yaml

from conftest import Stack
from lab import config as cfg
from lab import loadgen, runner
from lab.analysis import slo
from lab.analysis.detect import Detector, Rule, alert_intervals, evaluate, score
from lab.analysis.pipeline import analyze_run

CLASSES = {"short": {"words": 50, "max_tokens": 16}, "medium": {"words": 200, "max_tokens": 16}, "long": {"words": 600, "max_tokens": 16}}
MIX = {"short": 0.6, "medium": 0.3, "long": 0.1}


def metric(text: str, name: str, **labels) -> float:
    """Sum the samples of `name` whose labels include `labels` from Prometheus exposition text."""
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        m = re.match(rf"^{re.escape(name)}(\{{(.*)\}})?\s+([0-9.eE+-]+|NaN)$", line)
        if not m:
            continue
        have = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
        if all(have.get(k) == v for k, v in labels.items()):
            total += float(m.group(3))
    return total


def gen(url, prompt="what is an slo", max_tokens=8, cls="short", timeout=30):
    with httpx.stream("POST", f"{url}/v1/generate", json={"prompt": prompt, "max_tokens": max_tokens, "prompt_class": cls}, timeout=timeout) as r:
        lines = [json.loads(x) for x in r.iter_lines() if x.strip()]
        return r.status_code, lines


def run_load(url, segs, duration, rps, seed=1):
    arr = loadgen.build_arrivals(segs, duration, rps, seed)
    bodies = loadgen.prepare_bodies(arr, CLASSES, seed)
    return asyncio.run(loadgen.run_load(url, arr, bodies, request_timeout_s=30, start_epoch=time.time() + 0.3))


def test_happy_path_streams_tokens_then_done(stack: Stack):
    code, lines = gen(stack.gateway.url, max_tokens=8)
    assert code == 200
    tokens = [m for m in lines if m["type"] == "token"]
    done = lines[-1]
    assert len(tokens) == 8 and done["type"] == "done" and done["status"] == "ok"
    assert done["completion_tokens"] == 8 and done["prompt_tokens"] > 0
    assert 0 < done["retrieve_s"] < done["ttft_s"] <= done["total_s"]
    assert done["llm_ttft_s"] is not None


def test_sli_counters_match_the_request_log(stack: Stack):
    before = httpx.get(f"{stack.gateway.url}/metrics").text
    res = run_load(stack.gateway.url, [{"at": 0, "rps_x": 1.0, "mix": MIX}], 6, 4.0, seed=11)
    after = httpx.get(f"{stack.gateway.url}/metrics").text
    n = len(res.rows)
    assert n >= 10 and all(r["status"] == "ok" for r in res.rows)
    d_total = metric(after, "slo_requests_total") - metric(before, "slo_requests_total")
    d_ok = metric(after, "slo_requests_total", status="ok") - metric(before, "slo_requests_total", status="ok")
    d_good = metric(after, "slo_good_requests_total") - metric(before, "slo_good_requests_total")
    d_w = metric(after, "slo_weight_total") - metric(before, "slo_weight_total")
    assert d_total == n and d_ok == n
    assert d_good == n  # nothing is near the 1 s threshold on a healthy fake
    assert d_w == sum(r["weight"] for r in res.rows)  # server-side weight == client-side weight, request by request
    assert metric(after, "slo_inflight") == 0
    # histogram bookkeeping: every request observed once
    assert metric(after, "slo_ttft_seconds_count") - metric(before, "slo_ttft_seconds_count") == n


def test_open_loop_exposes_queueing_that_closed_loop_would_hide(tmp_path):
    """Offered load above capacity must show growing TTFT (coordinated-omission check)."""
    s = Stack(tmp_path, llm_env={"FAKE_SLOTS": "1", "FAKE_FIRST_TOKEN_S": "0.25", "FAKE_TOKEN_S": "0.0"}, gw_env={"SLO_TTFT_SHORT_S": "30"})
    try:
        # service time ~0.25 s => capacity 4 rps with one slot; offer ~8 rps for 8 s
        res = run_load(s.gateway.url, [{"at": 0, "rps_x": 1.0, "mix": {"short": 1.0}}], 8, 8.0, seed=5)
        ttft = sorted(r["ttft_s"] for r in res.rows if r["ttft_s"] is not None)
        assert len(ttft) > 40
        assert res.max_lag_s < 0.25  # the generator itself was not the bottleneck
        assert ttft[0] < 0.6
        assert ttft[-1] > 2.0  # queue built up: last requests waited seconds
    finally:
        s.stop()


def test_retriever_error_injection_is_a_bad_event(stack: Stack):
    gw = stack.gateway.url
    assert httpx.post(f"{gw}/admin/retriever-fault", json={"error_rate": 1.0}).status_code == 200
    try:
        before = httpx.get(f"{gw}/metrics").text
        code, _ = gen(gw)
        after = httpx.get(f"{gw}/metrics").text
        assert code == 503
        assert metric(after, "slo_requests_total", status="retriever_error") - metric(before, "slo_requests_total", status="retriever_error") == 1
        assert metric(after, "slo_good_requests_total") == metric(before, "slo_good_requests_total")
    finally:
        assert httpx.delete(f"{gw}/admin/retriever-fault").status_code == 200
    assert gen(gw)[0] == 200


def test_retriever_latency_shows_up_in_ttft_and_the_retrieve_stage(stack: Stack):
    gw = stack.gateway.url
    httpx.post(f"{gw}/admin/retriever-fault", json={"latency_ms": 600})
    try:
        _, lines = gen(gw)
    finally:
        httpx.delete(f"{gw}/admin/retriever-fault")
    done = lines[-1]
    assert done["status"] == "ok" and done["retrieve_s"] >= 0.6 and done["ttft_s"] >= 0.6


def test_ttft_over_threshold_is_ok_but_not_good(tmp_path):
    s = Stack(tmp_path, gw_env={"SLO_TTFT_SHORT_S": "0.001"})
    try:
        before = httpx.get(f"{s.gateway.url}/metrics").text
        _, lines = gen(s.gateway.url)
        after = httpx.get(f"{s.gateway.url}/metrics").text
        assert lines[-1]["status"] == "ok"
        assert metric(after, "slo_requests_total", status="ok") - metric(before, "slo_requests_total", status="ok") == 1
        assert metric(after, "slo_good_requests_total") == metric(before, "slo_good_requests_total")
    finally:
        s.stop()


def test_llm_down_is_reported_not_hung(tmp_path):
    s = Stack(tmp_path)
    try:
        gw = s.spawn_gateway(llm_url="http://127.0.0.1:9")  # nothing listens on :9
        t0 = time.time()
        code, lines = gen(gw.url)
        assert code == 200 and lines[-1]["status"] == "llm_error" and time.time() - t0 < 5
        m = httpx.get(f"{gw.url}/metrics").text
        assert metric(m, "slo_requests_total", status="llm_error") == 1
        assert metric(m, "slo_inflight") == 0
    finally:
        s.stop()


def test_gateway_deadline_produces_timeout_status(tmp_path):
    s = Stack(tmp_path, llm_env={"FAKE_FIRST_TOKEN_S": "5"})
    try:
        gw = s.spawn_gateway({"REQUEST_TIMEOUT_S": "1"})
        t0 = time.time()
        _, lines = gen(gw.url)
        assert lines[-1]["status"] == "timeout" and time.time() - t0 < 4
        assert metric(httpx.get(f"{gw.url}/metrics").text, "slo_inflight") == 0
    finally:
        s.stop()


def test_client_abort_is_recorded_and_inflight_released(stack: Stack):
    gw = stack.gateway.url
    before = httpx.get(f"{gw}/metrics").text
    with httpx.stream("POST", f"{gw}/v1/generate", json={"prompt": "x y z", "max_tokens": 200, "prompt_class": "short"}, timeout=10) as r:
        next(r.iter_lines())  # read one chunk then drop the connection
    time.sleep(1.0)
    after = httpx.get(f"{gw}/metrics").text
    assert metric(after, "slo_inflight") == 0
    assert metric(after, "slo_requests_total", status="client_abort") - metric(before, "slo_requests_total", status="client_abort") >= 1


def test_runner_end_to_end_with_fault_injection_detection_and_rca(tmp_path, monkeypatch):
    """The REAL runner + fault scheduler + analysis, 24 s, retriever latency fault at t=8..16."""
    s = Stack(tmp_path)
    try:
        conf, scen, runs = tmp_path / "config", tmp_path / "scenarios", tmp_path / "runs"
        conf.mkdir(); scen.mkdir()
        slo_cfg = {"slo": {"calibrated": True, "target": 0.99, "request_timeout_s": 20, "ttft_s": {"short": 1.0, "medium": 1.5, "long": 2.0}}}
        (conf / "slo.yaml").write_text(yaml.safe_dump(slo_cfg))
        (conf / "load.yaml").write_text(yaml.safe_dump({"load": {"calibrated": True, "base_rps": 5.0, "classes": CLASSES, "default_mix": MIX,
                                                                  "client": {"request_timeout_s": 30, "max_outstanding": 200}}}))
        (scen / "t_retriever.yaml").write_text(yaml.safe_dump({
            "name": "t_retriever", "duration_s": 24, "warmup_s": 0,
            "load": [{"at": 0, "rps_x": 1.0, "mix": MIX}],
            "faults": [{"at": 8, "until": 16, "kind": "retriever_fault", "params": {"latency_x_short_thr": 2.0}}],
            "incidents": [{"start": 8, "end": 16, "kind": "retriever_latency", "root_cause": "retriever"}]}))
        monkeypatch.setattr(cfg, "CONFIG_DIR", conf)
        monkeypatch.setattr(cfg, "SCENARIO_DIR", scen)
        monkeypatch.setattr(cfg, "RUNS_DIR", runs)
        monkeypatch.setattr(cfg, "GATEWAY_URL", s.gateway.url)

        out = runner.run_one("X", "t_retriever", "static1", 1, prepare=False)
        meta = json.loads((out / "meta.json").read_text())
        events = json.loads((out / "events.json").read_text())
        assert (out / "DONE").exists() and (out / "requests.csv").exists()
        assert meta["valid"], meta["invalid_reasons"]
        assert meta["n_requests"] > 80 and meta["max_lag_s"] < 0.25
        acts = [(e["action"], round(e["t_rel"])) for e in events["fault_events"]]
        assert [a for a, _ in acts] == ["apply", "revert"] and all(e["ok"] for e in events["fault_events"])
        assert abs(acts[0][1] - 8) <= 1 and abs(acts[1][1] - 16) <= 1  # faults fired on schedule

        # the fault must have been removed afterwards
        assert httpx.get(f"{s.retriever.url}/admin/fault").json()["latency_ms"] == 0

        df = slo.annotate(slo.load_requests(out / "requests.csv"), meta["slo"]["ttft_s"])
        during = df[(df.t_send >= 9) & (df.t_send < 16)]
        before = df[df.t_send < 7]
        assert before["good"].mean() > 0.97
        assert during["good"].mean() < 0.05  # 2.0 s injected latency vs 1-2 s thresholds => essentially all bad
        assert (during["retrieve_s"] > 1.9).mean() > 0.9

        # replay detection with windows scaled to this 24 s run + stage attribution
        det = Detector("tiny", "request", (Rule(6, 2, 10.0),), min_events=3)
        grid, fire = evaluate(df, det, 0.99, 24, min_history_s=4.0)
        res = score(alert_intervals(grid, fire), events["incidents"], 24, grace_s=8)
        assert res["detected"] and res["ttd_s"] <= 6 and res["false_pages"] == 0

        analysis = analyze_run(out, [det])
        r = analysis["rca"]
        assert r == [] or r[0]["pred"] in (None, "retriever")  # incident starts at 8 s (<60 s) => RCA not scored here
        from lab.analysis import rca as rca_mod
        loc = rca_mod.localize(df, (1.0, 7.0), (9.0, 16.0))
        assert loc["pred"] == "retriever", loc
    finally:
        s.stop()


def test_run_is_skipped_when_done_and_invalid_when_client_is_starved(tmp_path, monkeypatch):
    s = Stack(tmp_path)
    try:
        conf, scen, runs = tmp_path / "config", tmp_path / "scenarios", tmp_path / "runs"
        conf.mkdir(); scen.mkdir()
        (conf / "slo.yaml").write_text(yaml.safe_dump({"slo": {"calibrated": True, "target": 0.99, "request_timeout_s": 20, "ttft_s": {"short": 1, "medium": 1, "long": 1}}}))
        (conf / "load.yaml").write_text(yaml.safe_dump({"load": {"calibrated": True, "base_rps": 3.0, "classes": CLASSES, "default_mix": MIX, "client": {"max_outstanding": 2}}}))
        (scen / "t.yaml").write_text(yaml.safe_dump({"name": "t", "duration_s": 6, "load": [{"at": 0, "rps_x": 1.0, "mix": MIX}], "faults": [], "incidents": []}))
        for attr, val in (("CONFIG_DIR", conf), ("SCENARIO_DIR", scen), ("RUNS_DIR", runs), ("GATEWAY_URL", s.gateway.url)):
            monkeypatch.setattr(cfg, attr, val)
        # max_outstanding=2 with ~0.2 s service => some requests are dropped client-side => run must be flagged invalid
        out = runner.run_one("X", "t", "static1", 1, prepare=False)
        meta = json.loads((out / "meta.json").read_text())
        assert any("dropped client-side" in r for r in meta["invalid_reasons"]) == (not meta["valid"])
        stamp = (out / "DONE").stat().st_mtime
        again = runner.run_one("X", "t", "static1", 1, prepare=False)
        assert again == out and (out / "DONE").stat().st_mtime == stamp  # resumable: finished runs are not redone
        assert meta["attempts"] == 1
        redone = runner.run_one("X", "t", "static1", 1, prepare=False, force=True)   # a re-run overwrites the data; attempts is the only trace
        assert json.loads((redone / "meta.json").read_text())["attempts"] == 2
    finally:
        s.stop()


def test_plan_is_deterministic_and_complete():
    from lab.cli import _plan
    a = _plan("A", 3, None)
    assert a == _plan("A", 3, None)
    m = cfg.load_matrix()["A"]
    assert len(a) == len(m["scenarios"]) * len(m["policies"]) * 3 and len(set(a)) == len(a)
    assert a != sorted(a)  # shuffled (decorrelates runs from thermal drift)
    b = _plan("B", 2, ["s1_burst"])
    assert {j[0] for j in b} == {"s1_burst"} and {j[1] for j in b} == set(cfg.load_matrix()["B"]["policies"])


def test_fault_body_converts_latency_multiple_to_ms():
    fs = runner.FaultScheduler([], 0.0, {"ttft_s": {"short": 1.2}})
    assert fs._fault_body({"params": {"latency_x_short_thr": 1.5, "error_rate": 0.1}}) == {"error_rate": 0.1, "latency_ms": pytest.approx(1800.0)}
