"""Behavioural test of every dashboard query, with a real Prometheus engine (`promtool test rules`).

tests/test_chart.py proves the queries PARSE and use series that exist. That cannot catch a query that parses and
then returns the wrong number (a mismatched label join, a missing `> 0` guard, a unit slip, a recording rule that is
not what the dashboard thinks it is). Here the chart's real PrometheusRule is evaluated by Prometheus' own engine over
synthetic counters whose totals are known by construction, and every panel query must return the number we can work out
by hand. A query with no expectation fails the test, so a new panel cannot ship untested.

Needs the `promtool` binary (https://github.com/prometheus/prometheus/releases; CI downloads it). Skipped without it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from lab import dashgen
from test_chart import by_kind, docs, load_values, renderer  # noqa: F401  (renderer is a pytest fixture)

PROMTOOL = os.environ.get("PROMTOOL") or shutil.which("promtool")
pytestmark = [
    pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain needed for the offline chart renderer"),
    # Locally the test is skipped without promtool; in CI (CI=true) it is NOT skipped, so a broken install step fails loudly.
    pytest.mark.skipif(PROMTOOL is None and not os.environ.get("CI"), reason="promtool not installed (set PROMTOOL=/path/to/promtool)"),
]


def test_promtool_is_installed_in_ci():
    if os.environ.get("CI"):
        assert PROMTOOL and Path(PROMTOOL).exists(), "CI must install promtool (see .github/workflows/ci.yml)"

TICKS = 240            # 5 s ticks => 20 minutes of data
EVAL = "20m"
RANGE, RANGE_S = "10m", "600"   # what Grafana substitutes for $__range / $__range_s on a 10-minute dashboard

# Synthetic traffic, per 5 s tick. Totals by class: short 1 ok (all good); medium 1 ok (half good); long 1 ok (none good) + 0.5 llm_error.
#   all requests 3.5/tick = 0.7/s, good 1.5/tick = 0.3/s  =>  good ratio 3/7, burn (target 0.99) 57.142857x
#   weights: short 1, medium 2, long 6 (all of it); good weight 1 + 1 + 0  =>  token good ratio 2/9
SERIES: dict[str, str] = {
    'slo_requests_total{class="short",status="ok"}': "0+1x%d",
    'slo_requests_total{class="medium",status="ok"}': "0+1x%d",
    'slo_requests_total{class="long",status="ok"}': "0+1x%d",
    'slo_requests_total{class="long",status="llm_error"}': "0+0.5x%d",
    'slo_good_requests_total{class="short"}': "0+1x%d",
    'slo_good_requests_total{class="medium"}': "0+0.5x%d",
    'slo_good_requests_total{class="long"}': "0+0x%d",
    'slo_weight_total{class="short"}': "0+1x%d",
    'slo_weight_total{class="medium"}': "0+2x%d",
    'slo_weight_total{class="long"}': "0+6x%d",
    'slo_good_weight_total{class="short"}': "0+1x%d",
    'slo_good_weight_total{class="medium"}': "0+1x%d",
    'slo_good_weight_total{class="long"}': "0+0x%d",
    "slo_inflight": "5+0x%d",
    # TTFT histograms: short all <= 0.5 s, medium all in (1, 2] s, long all above 2 s
    **{f'slo_ttft_seconds_bucket{{class="{c}",le="{le}"}}': f"0+{n}x%d" for c, row in {
        "short": {"0.5": 1, "1": 1, "2": 1, "+Inf": 1},
        "medium": {"0.5": 0, "1": 0, "2": 1, "+Inf": 1},
        "long": {"0.5": 0, "1": 0, "2": 0, "+Inf": 1},
    }.items() for le, n in row.items()},
    # stage histograms: retrieve all <= 10 ms, llm_ttft all in (0.5, 1] s
    **{f'slo_stage_seconds_bucket{{stage="{s}",le="{le}"}}': f"0+{n}x%d" for s, row in {
        "retrieve": {"0.01": 1, "0.1": 1, "+Inf": 1},
        "llm_ttft": {"0.5": 0, "1": 1, "+Inf": 1},
    }.items() for le, n in row.items()},
    'retriever_duration_seconds_bucket{le="0.01"}': "0+1x%d",
    'retriever_duration_seconds_bucket{le="0.1"}': "0+1x%d",
    'retriever_duration_seconds_bucket{le="+Inf"}': "0+1x%d",
    # platform series: 2 of 3 desired llm replicas ready; 2 pods each using 0.5 core of a 1-core request
    'kube_deployment_status_replicas_ready{namespace="slo-loop",deployment="llm"}': "2+0x%d",
    'kube_deployment_spec_replicas{namespace="slo-loop",deployment="llm"}': "3+0x%d",
    'container_cpu_usage_seconds_total{namespace="slo-loop",pod="llm-a",container="llama"}': "0+2.5x%d",
    'container_cpu_usage_seconds_total{namespace="slo-loop",pod="llm-b",container="llama"}': "0+2.5x%d",
    'kube_pod_container_resource_requests{namespace="slo-loop",pod="llm-a",container="llama",resource="cpu",unit="core"}': "1+0x%d",
    'kube_pod_container_resource_requests{namespace="slo-loop",pod="llm-b",container="llama",resource="cpu",unit="core"}': "1+0x%d",
}

BURN = (1 - 3 / 7) / 0.01
TOKEN_BURN = (1 - 2 / 9) / 0.01
Sample = tuple[dict, float]


def n(name: str, **labels: str) -> dict:
    return {"__name__": name, **labels}


def _expect() -> dict[str, list[Sample]]:
    """expression text -> the samples it must return. Keys reuse the generator's own constants wherever it has them."""
    E: dict[str, list[Sample]] = {}
    E[dashgen.BUDGET_LEFT] = [({}, 1 - BURN)]
    E[dashgen.RANGE_GOOD] = [({}, 3 / 7)]
    E[dashgen.GOOD_RATIO_120S] = [({}, 3 / 7)]
    E[dashgen.BUDGET_LEFT_BY_CLASS] = [({"class": "short"}, 1.0), ({"class": "medium"}, -49.0), ({"class": "long"}, -99.0)]
    E[dashgen.BURN_BY_CLASS] = [({"class": "short"}, 0.0), ({"class": "medium"}, 50.0), ({"class": "long"}, 100.0)]
    E[dashgen.QUEUE_DEPTH] = [({}, 1.0)]                      # 5 in flight - 4 slots (2 ready x parallel 2)
    E[dashgen.READY] = [({}, 2.0)]
    E[dashgen.DESIRED] = [({}, 3.0)]
    E[f"{dashgen.DESIRED} - {dashgen.READY}"] = [({}, 1.0)]
    for w, v in (("30s", BURN), ("120s", BURN), ("300s", BURN)):
        E[f"slo:burn_rate:request_{w}"] = [(n(f"slo:burn_rate:request_{w}"), v)]
    E["slo:burn_rate:token_120s"] = [(n("slo:burn_rate:token_120s"), TOKEN_BURN)]
    E["max(slo:burn_rate:request_30s)"] = [({}, BURN)]
    E['count(ALERTS{alertname=~"SLOBurn.*",alertstate="firing"}) or vector(0)'] = [({}, 3.0)]   # SLOBurnFast, SLOBurnSlow, SLOBurnFastToken
    firing = [(n("ALERTS", alertname=a, alertstate="firing", severity=sev), 1.0)
              for a, sev in (("SLOBurnFast", "page"), ("SLOBurnFastToken", "page"), ("SLOBurnSlow", "ticket"))]
    E['ALERTS{alertname=~"SLOBurn.*",alertstate=~"pending|firing"}'] = firing
    E['ALERTS{alertname=~"SLOBurn.*",alertstate="firing"}'] = firing
    E["sum(rate(slo_requests_total[1m]))"] = [({}, 0.7)]
    E["sum(rate(slo_good_requests_total[30s]))"] = [({}, 0.3)]
    E['clamp_min(sum(rate(slo_requests_total{status="ok"}[30s])) - sum(rate(slo_good_requests_total[30s])), 0)'] = [({}, 0.3)]
    E['sum(rate(slo_requests_total{status!="ok"}[30s])) or vector(0)'] = [({}, 0.1)]
    E['sum by (status) (rate(slo_requests_total{status!="ok"}[30s]))'] = [({"status": "llm_error"}, 0.1)]
    E["histogram_quantile(0.95, sum by (le, class) (rate(slo_ttft_seconds_bucket[1m])))"] = [
        ({"class": "short"}, 0.475), ({"class": "medium"}, 1.95), ({"class": "long"}, 2.0)]
    E['histogram_quantile(0.95, sum by (le) (rate(slo_ttft_seconds_bucket{class="short"}[1m])))'] = [({}, 0.475)]
    E["histogram_quantile(0.95, sum by (le, stage) (rate(slo_stage_seconds_bucket[1m])))"] = [({"stage": "retrieve"}, 0.0095), ({"stage": "llm_ttft"}, 0.975)]
    E["histogram_quantile(0.95, sum by (le) (rate(retriever_duration_seconds_bucket[1m])))"] = [({}, 0.0095)]
    E["slo:good_ratio:class_120s"] = [(n("slo:good_ratio:class_120s", **{"class": c}), v) for c, v in (("short", 1.0), ("medium", 0.5), ("long", 0.0))]
    E["slo:target_ratio"] = [(n("slo:target_ratio"), 0.99)]
    E["slo:ttft_threshold_seconds"] = [(n("slo:ttft_threshold_seconds", **{"class": c}), v) for c, v in (("long", 16.3), ("medium", 15.6), ("short", 10.4))]
    E["sum(slo_inflight)"] = [({}, 5.0)]
    E["slo:llm_slots"] = [(n("slo:llm_slots"), 4.0)]
    E["slo:autoscaling_policy_info"] = [(n("slo:autoscaling_policy_info", policy="cpu"), 1.0)]
    E[f"sum(rate(container_cpu_usage_seconds_total{{{dashgen.LLM_POD}}}[1m]))"] = [({}, 1.0)]
    E[f'sum(kube_pod_container_resource_requests{{{dashgen.LLM_POD},resource="cpu"}})'] = [({}, 2.0)]
    E[f'sum(rate(container_cpu_usage_seconds_total{{{dashgen.LLM_POD}}}[1m])) / sum(kube_pod_container_resource_requests{{{dashgen.LLM_POD},resource="cpu"}})'] = [({}, 0.5)]
    E[f"sum(slo_inflight) / (sum(kube_deployment_status_replicas_ready{{{dashgen.LLM}}}) > 0)"] = [({}, 2.5)]
    E[f"sum(rate({dashgen.TOTAL}[1m])) / (sum(kube_deployment_status_replicas_ready{{{dashgen.LLM}}}) > 0)"] = [({}, 0.35)]
    E[f"sum(avg_over_time(kube_deployment_status_replicas_ready{{{dashgen.LLM}}}[$__range])) * $__range_s / 60"] = [({}, 20.0)]   # 2 replicas x 10 min
    for sig, v in (("cpu", 0.6), ("queue", 2.0), ("burn", 2.0)):
        E[f'slo:scaling_threshold{{signal="{sig}"}}'] = [(n("slo:scaling_threshold", signal=sig), v)]
    return E


def _rounded(expr: str) -> str:
    """promtool compares floats exactly, and Prometheus itself is only exact to ~1e-15, so every query is wrapped in round(..., 1e-6)
    (1 / 1e-6 is exactly 1e6, so the engine and Python's round(x * 1e6) / 1e6 agree to the last bit). round() drops __name__."""
    return f"round(({_sub(expr)}), 1e-06)"


def _sub(expr: str) -> str:
    return re.sub(r"\$__rate_interval|\$__interval", "1m", expr.replace("$__range_s", RANGE_S).replace("$__range", RANGE))


def _label_str(d: dict) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}"


def _dashboard_exprs() -> dict[str, list[str]]:
    """expression -> where it is used"""
    out: dict[str, list[str]] = {}
    for name in dashgen.DASHBOARDS:
        d = json.loads((dashgen.OUT_DIR / name).read_text())
        for p in d["panels"]:
            for t in p.get("targets", []):
                out.setdefault(t["expr"], []).append(f"{name}:{p['title']}:{t['refId']}")
        for a in d["annotations"]["list"]:
            if a.get("expr"):
                out.setdefault(a["expr"], []).append(f"{name}:annotation:{a['name']}")
    return out


@pytest.fixture(scope="module")
def promtool_result(renderer, tmp_path_factory):  # noqa: F811
    tmp = tmp_path_factory.mktemp("promtool")
    ds = docs(renderer, load_values("cpu"), tmp)
    rule = by_kind(ds, "PrometheusRule")["slo-loop"]
    (tmp / "rules.yaml").write_text(yaml.safe_dump({"groups": rule["spec"]["groups"]}, sort_keys=False))
    expect = _expect()
    tests = [{
        "interval": "5s",
        "input_series": [{"series": s, "values": v % TICKS} for s, v in SERIES.items()],
        "promql_expr_test": [
            {"expr": _rounded(expr), "eval_time": EVAL,
             "exp_samples": [{"labels": _label_str({k: v for k, v in lbl.items() if k != "__name__"}), "value": round(val * 1e6) / 1e6}
                             for lbl, val in samples]}
            for expr, samples in expect.items()],
    }]
    (tmp / "test.yaml").write_text(yaml.safe_dump({"rule_files": ["rules.yaml"], "evaluation_interval": "5s", "tests": tests}, sort_keys=False, width=10_000))
    p = subprocess.run([PROMTOOL, "check", "rules", str(tmp / "rules.yaml")], capture_output=True, text=True)
    t = subprocess.run([PROMTOOL, "test", "rules", "test.yaml"], cwd=tmp, capture_output=True, text=True)
    return p, t


# Idle lab: the counters exist but nothing is happening. A tile must then say "no data", not "NaN" and not "0 %" (0 % compliance
# would read as a total outage), which is what the `> 0` guards in the queries are for. Burn rate is the documented exception: 0.
IDLE_SERIES = {k: "0+0x%d" for k in SERIES if k.startswith(("slo_requests_total", "slo_good_requests_total", "slo_weight_total", "slo_good_weight_total"))}


def _idle_expect() -> dict[str, list[Sample]]:
    E: dict[str, list[Sample]] = {}
    for q in (dashgen.RANGE_GOOD, dashgen.BUDGET_LEFT, dashgen.BUDGET_LEFT_BY_CLASS, dashgen.GOOD_RATIO_120S, dashgen.BURN_BY_CLASS,
              "slo:good_ratio:class_120s"):
        E[q] = []
    E["slo:burn_rate:request_120s"] = [(n("slo:burn_rate:request_120s"), 0.0)]
    return E


@pytest.fixture(scope="module")
def promtool_idle_result(renderer, tmp_path_factory):  # noqa: F811
    tmp = tmp_path_factory.mktemp("promtool-idle")
    ds = docs(renderer, load_values("cpu"), tmp)
    rule = by_kind(ds, "PrometheusRule")["slo-loop"]
    (tmp / "rules.yaml").write_text(yaml.safe_dump({"groups": rule["spec"]["groups"]}, sort_keys=False))
    tests = [{
        "interval": "5s",
        "input_series": [{"series": s, "values": v % TICKS} for s, v in IDLE_SERIES.items()],
        "promql_expr_test": [
            {"expr": _rounded(expr), "eval_time": EVAL,
             "exp_samples": [{"labels": _label_str({k: v for k, v in lbl.items() if k != "__name__"}), "value": round(val * 1e6) / 1e6} for lbl, val in samples]}
            for expr, samples in _idle_expect().items()],
    }]
    (tmp / "test.yaml").write_text(yaml.safe_dump({"rule_files": ["rules.yaml"], "evaluation_interval": "5s", "tests": tests}, sort_keys=False, width=10_000))
    return subprocess.run([PROMTOOL, "test", "rules", "test.yaml"], cwd=tmp, capture_output=True, text=True)


def test_an_idle_lab_shows_no_data_not_nan_and_not_zero_percent(promtool_idle_result):
    t = promtool_idle_result
    assert t.returncode == 0, "with no traffic a ratio tile must be empty (and burn rate 0):\n" + t.stdout + t.stderr


def test_the_rendered_rules_pass_promtool_check(promtool_result):
    p, _ = promtool_result
    assert p.returncode == 0, p.stdout + p.stderr


def test_every_dashboard_query_has_a_hand_computed_expectation():
    used = {_sub(e) for e in _dashboard_exprs()}
    expected = {_sub(e) for e in _expect()}
    missing = sorted(used - expected)
    assert not missing, "dashboard queries without a hand-computed expectation (add them to _expect()):\n" + "\n".join(missing)


def test_every_dashboard_query_returns_the_hand_computed_value(promtool_result):
    _, t = promtool_result
    assert t.returncode == 0, "promtool says a dashboard query returned something other than the hand-computed value:\n" + t.stdout + t.stderr
