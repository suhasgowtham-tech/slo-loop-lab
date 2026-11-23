"""Grafana fault/run markers. The one property that matters more than the markers themselves: they can never disturb a run."""
from __future__ import annotations

import time

import pytest
from fake_grafana import FakeGrafana

from lab import annotate, cli, runner

AUTH = ("admin", "pw")


@pytest.fixture
def grafana():
    g = FakeGrafana()
    yield g
    g.close()


def _annotator(url, **kw):
    msgs: list[str] = []
    kw.setdefault("timeout_s", 1.0)
    return annotate.Annotator(url, AUTH, enabled=kw.pop("enabled", True), say=msgs.append, **kw), msgs


def test_a_marker_is_posted_then_closed_into_a_region(grafana):
    a, _ = _annotator(grafana.url)
    t0 = time.time()
    m = a.start("Fault: retriever_fault (latency_ms=15600)", ["fault", "retriever_fault"], at=t0)
    a.end(m, at=t0 + 60)
    assert a.flush()
    post, put = grafana.requests
    assert post["method"] == "POST" and post["path"] == "/api/annotations"
    assert post["body"]["tags"] == ["slo-loop", "fault", "retriever_fault"]      # the dashboards select on exactly these tags
    assert post["body"]["time"] == round(t0 * 1000) and "timeEnd" not in post["body"]
    assert put["method"] == "PUT" and put["path"] == "/api/annotations/1"
    assert put["body"]["time"] == round(t0 * 1000) and put["body"]["timeEnd"] == round((t0 + 60) * 1000)
    assert put["body"]["tags"] == post["body"]["tags"] and put["body"]["text"] == post["body"]["text"]
    assert post["auth"].startswith("Basic ") and post["auth"] == put["auth"]


def test_dashboards_select_the_tags_the_harness_posts():
    """The tag contract between lab/annotate.py + lab/runner.py and lab/dashgen.py, checked from both ends."""
    from lab import dashgen
    ann = {a["name"]: a for a in dashgen.health()["annotations"]["list"] if "target" in a}
    tags = sorted(tuple(a["target"]["tags"]) for a in ann.values())
    assert tags == [("slo-loop", "fault"), ("slo-loop", "run")]
    assert annotate.TAG == dashgen.TAG == "slo-loop"


def test_disabled_does_nothing_at_all(grafana):
    a, _ = _annotator(grafana.url, enabled=False)
    assert a.start("x", ["fault"]) is None
    a.end(None)
    assert a.flush() and grafana.requests == []


@pytest.mark.parametrize("value,expected", [("0", False), ("false", False), ("OFF", False), ("1", True), ("", True)])
def test_env_switch(monkeypatch, value, expected):
    monkeypatch.setenv("LAB_ANNOTATE", value)
    assert annotate.enabled_by_env() is expected


def test_an_unreachable_grafana_never_raises_never_blocks_and_warns_once():
    a, msgs = _annotator("http://127.0.0.1:9", timeout_s=0.5)    # port 9 (discard): connection refused
    t0 = time.time()
    handles = [a.start(f"f{i}", ["fault"]) for i in range(5)]
    for h in handles:
        a.end(h)
    assert time.time() - t0 < 0.2, "start()/end() must return immediately; the HTTP work is on a background thread"
    assert a.flush(5)
    assert a.failures >= annotate.MAX_CONSECUTIVE_FAILURES and len(msgs) == 1 and "not affected" in msgs[0]


def test_a_slow_grafana_does_not_slow_the_caller():
    g = FakeGrafana(delay_s=1.0)
    try:
        a, _ = _annotator(g.url, timeout_s=3.0)
        t0 = time.time()
        a.end(a.start("slow", ["fault"]))
        assert time.time() - t0 < 0.2
        assert a.flush(10)
    finally:
        g.close()


def test_a_failed_post_means_no_put_and_a_later_success_resets_the_counter():
    g = FakeGrafana(fail_posts=1)
    try:
        a, msgs = _annotator(g.url)
        first = a.start("will fail", ["fault"])
        a.end(first)
        second = a.start("works", ["fault"])
        a.end(second)
        assert a.flush()
        methods = [(r["method"], r["path"]) for r in g.requests]
        assert methods == [("POST", "/api/annotations"), ("POST", "/api/annotations"), ("PUT", "/api/annotations/1")], methods
        assert first.id is None and second.id == 1 and a.failures == 0 and msgs == []
    finally:
        g.close()


def test_post_now_raises_because_the_person_asked_for_it(grafana):
    assert annotate.post_now("deployed v2", ["deploy"], base_url=grafana.url, auth=AUTH) == 1
    assert grafana.requests[0]["body"]["tags"] == ["slo-loop", "deploy"]
    with pytest.raises(Exception):  # noqa: B017 - any connection error
        annotate.post_now("x", [], base_url="http://127.0.0.1:9", auth=AUTH, timeout_s=0.5)


def test_cli_annotate(monkeypatch, capsys, grafana):
    monkeypatch.setattr(annotate.cfg, "GRAFANA_URL", grafana.url)
    assert cli.main(["annotate", "deployed v2", "--tag", "deploy"]) == 0
    assert "marker 1 added" in capsys.readouterr().out
    monkeypatch.setattr(annotate.cfg, "GRAFANA_URL", "http://127.0.0.1:9")
    assert cli.main(["annotate", "x"]) == 1
    assert "Is the lab up" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ FaultScheduler hook
def _scheduler(monkeypatch, annotator, faults, *, fail_apply=False):
    s = runner.FaultScheduler(faults, time.time() + 0.1, {"ttft_s": {"short": 1.0}}, annotator, "s5_retriever_latency / cpu / r1")
    monkeypatch.setattr(s, "_apply", (lambda f: (_ for _ in ()).throw(RuntimeError("kubectl failed"))) if fail_apply else (lambda f: None))
    monkeypatch.setattr(s, "_revert", lambda f: None)
    return s


FAULT = {"at": 0.2, "until": 0.7, "kind": "retriever_fault", "params": {"latency_x_short_thr": 1.5}}


def test_scheduler_marks_each_applied_fault_as_a_region(monkeypatch, grafana):
    a, _ = _annotator(grafana.url)
    s = _scheduler(monkeypatch, a, [FAULT])
    s.start()
    s.join(10)
    assert a.flush()
    (ann,) = grafana.annotations.values()
    assert ann["tags"] == ["slo-loop", "fault", "retriever_fault"]
    assert "latency_x_short_thr=1.5" in ann["text"] and "s5_retriever_latency / cpu / r1" in ann["text"]
    assert 0.4 <= (ann["timeEnd"] - ann["time"]) / 1000 <= 1.5                       # about the 0.5 s the fault was on
    assert abs(ann["time"] / 1000 - (s.start_epoch + 0.2)) < 0.5                     # timed when it was applied, not when the HTTP call finished


def test_a_fault_that_failed_to_apply_gets_no_marker(monkeypatch, grafana):
    a, _ = _annotator(grafana.url)
    s = _scheduler(monkeypatch, a, [FAULT], fail_apply=True)
    s.start()
    s.join(10)
    a.flush()
    assert grafana.requests == [] and s.errors and "kubectl failed" in s.errors[0]


def test_a_run_stopped_in_the_middle_of_a_fault_closes_the_marker(monkeypatch, grafana):
    a, _ = _annotator(grafana.url)
    s = _scheduler(monkeypatch, a, [{**FAULT, "at": 0.1, "until": 60}])
    s.start()
    time.sleep(0.6)
    s.stop()
    s.join(5)
    assert a.flush()
    (ann,) = grafana.annotations.values()
    assert "timeEnd" in ann, "an aborted run must not leave an open-ended fault band"


def test_the_scheduler_still_works_without_an_annotator(monkeypatch):
    s = _scheduler(monkeypatch, None, [FAULT])
    s.start()
    s.join(10)
    assert [e["action"] for e in s.events] == ["apply", "revert"] and not s.errors


def test_a_dead_grafana_does_not_change_when_faults_are_applied(monkeypatch):
    """The measurement contract: fault times come from the absolute schedule, never from how long Grafana takes."""
    a, _ = _annotator("http://127.0.0.1:9", timeout_s=0.5)
    s = _scheduler(monkeypatch, a, [FAULT, {**FAULT, "at": 0.7, "until": 1.0}])   # the second starts the instant the first ends
    s.start()
    s.join(10)
    sched = [("apply", 0.2), ("revert", 0.7), ("apply", 0.7), ("revert", 1.0)]
    assert [(e["action"], ) for e in s.events] == [(a_, ) for a_, _ in sched]
    for e, (_, due) in zip(s.events, sched):
        assert 0 <= e["t_rel"] - due < 0.3, f"{e['action']} ran {e['t_rel'] - due:.2f} s after its scheduled time"
