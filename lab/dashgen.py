"""Grafana dashboards as code.

`python -m lab.dashgen` (or `make dashboards-build`) writes the three provisioned dashboards to
charts/slo-loop-app/dashboards/*.json. The JSON is committed (the chart ships it), and
tests/test_dashboards.py fails if the committed files differ from what this module generates, so the
generator and the JSON cannot drift apart.

  slo-loop-health        Service health        "Are we meeting the SLO, and how much error budget is left?"  (VPs, founders)
  slo-loop-incident      Incident view         "What broke, where, and since when?"                          (on-call engineers)
  slo-loop-autoscaling   Autoscaling           "Does capacity follow demand, and what does it cost?"         (the lab's experiment)

Design rules (the same ones the tests enforce):
  * every query uses a metric the apps export, a recording rule the chart defines, or a kube-state-metrics/cAdvisor series;
  * thresholds that exist in the alert rules or the chart values (6x / 2.5x burn, TTFT per class, SLO target, KEDA
    thresholds) are NOT retyped here: they are drawn from recording rules, so a dashboard can never disagree with the alert;
  * colour follows the entity, never the rank: a class (short/medium/long) has the same colour on every panel; good/slow/
    failed and the status colours are fixed;
  * markers on every time series: injected faults and experiment runs (posted by the harness, lab/annotate.py) and the
    burn-rate alerts (read from Prometheus' ALERTS series).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "charts" / "slo-loop-app" / "dashboards"

NS = "slo-loop"                       # == lab.config.NAMESPACE (tests/test_dashboards.py checks)
DS = {"type": "prometheus", "uid": "${datasource}"}
GRAFANA_DS = {"type": "grafana", "uid": "-- Grafana --"}
TAG = "slo-loop"

# ----------------------------------------------------------------------------------------------- colours
# Status colours are fixed (good / warning / serious / critical). Categorical colours follow the entity.
GOOD, WARN, SERIOUS, CRIT = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
BLUE, ORANGE, AQUA, VIOLET, GOLD = "#3987e5", "#d95926", "#199e70", "#9085e9", "#c98500"
BLUE_LIGHT = "#9ec5f4"
CLASS_COLOR = {"short": BLUE, "medium": ORANGE, "long": AQUA}
STAGE_COLOR = {"retrieve": VIOLET, "llm_ttft": GOLD}

# ----------------------------------------------------------------------------------------------- PromQL building blocks
LLM = f'namespace="{NS}",deployment="llm"'
LLM_POD = f'namespace="{NS}",pod=~"llm-.*",container="llama"'
TOTAL = "slo_requests_total"
GOODC = "slo_good_requests_total"

# fraction of requests that met the SLO, over the dashboard's time range (an instant query)
RANGE_GOOD = f"sum(increase({GOODC}[$__range])) / (sum(increase({TOTAL}[$__range])) > 0)"
RANGE_GOOD_BY_CLASS = f"sum by (class) (increase({GOODC}[$__range])) / (sum by (class) (increase({TOTAL}[$__range])) > 0)"
# share of the error budget NOT yet spent: 100 % = no bad request, 0 % = SLO exactly met, < 0 = SLO missed
BUDGET_LEFT = f"1 - (1 - ({RANGE_GOOD})) / scalar(1 - slo:target_ratio)"
BUDGET_LEFT_BY_CLASS = f"1 - (1 - ({RANGE_GOOD_BY_CLASS})) / scalar(1 - slo:target_ratio)"
GOOD_RATIO_120S = f"sum(rate({GOODC}[120s])) / (sum(rate({TOTAL}[120s])) > 0)"
READY = f"sum(kube_deployment_status_replicas_ready{{{LLM}}})"
DESIRED = f"sum(kube_deployment_spec_replicas{{{LLM}}})"
QUEUE_DEPTH = "clamp_min(sum(slo_inflight) - slo:llm_slots, 0)"
BURN_BY_CLASS = (
    f"(1 - sum by (class) (rate({GOODC}[120s])) / (sum by (class) (rate({TOTAL}[120s])) > 0)) / scalar(1 - slo:target_ratio)"
)

# Burn-rate bands mirror the alert rules (SLOBurnSlow = 2.5x, SLOBurnFast = 6x): green < 1x, yellow < 2.5x, orange < 6x, red.
BURN_STEPS = [(GOOD, None), (WARN, 1), (SERIOUS, 2.5), (CRIT, 6)]
BUDGET_STEPS = [(CRIT, None), (SERIOUS, 0), (GOOD, 0.25)]


# ----------------------------------------------------------------------------------------------- panel helpers
def _steps(steps: list[tuple[str, float | None]]) -> dict:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def _target(ref: str, expr: str, legend: str | None = None, *, instant: bool = False) -> dict:
    t: dict[str, Any] = {"datasource": DS, "editorMode": "code", "expr": expr, "refId": ref}
    if instant:
        t["instant"], t["range"] = True, False
    else:
        t["range"] = True
    if legend is not None:
        t["legendFormat"] = legend
    return t


def _override(matcher: str, options: str, props: dict[str, Any]) -> dict:
    return {"matcher": {"id": matcher, "options": options}, "properties": [{"id": k, "value": v} for k, v in props.items()]}


def fixed(color: str) -> dict:
    return {"mode": "fixed", "fixedColor": color}


DASHED = {"fill": "dash", "dash": [10, 10]}


class Board:
    """Flow layout: panels go left to right and wrap at 24 columns; a row header starts a new line."""

    def __init__(self, uid: str, title: str, description: str, tags: list[str], *, refresh: str, range_from: str):
        self.uid, self.title, self.description, self.tags = uid, title, description, tags
        self.refresh, self.range_from = refresh, range_from
        self.panels: list[dict] = []
        self._id = 0
        self._x = 0
        self._y = 0
        self._line_h = 0

    def _place(self, w: int, h: int) -> dict:
        if self._x + w > 24:
            self._y += self._line_h
            self._x, self._line_h = 0, 0
        pos = {"h": h, "w": w, "x": self._x, "y": self._y}
        self._x += w
        self._line_h = max(self._line_h, h)
        return pos

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def row(self, title: str) -> None:
        self._y += self._line_h
        self._x, self._line_h = 0, 0
        self.panels.append({"collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y}, "id": self._next_id(),
                            "panels": [], "title": title, "type": "row"})
        self._y += 1

    def stat(self, title: str, targets: list[dict], *, w: int = 4, h: int = 4, unit: str = "short", steps=None, decimals: int | None = None,
             description: str = "", color_mode: str = "background", graph: bool = False, text_mode: str = "auto", no_value: str = "no data",
             mappings: list | None = None, minimum: float | None = None) -> None:
        defaults: dict[str, Any] = {
            "color": {"mode": "thresholds"} if steps else fixed(BLUE),
            "mappings": mappings or [], "noValue": no_value,
            "thresholds": _steps(steps or [(BLUE, None)]), "unit": unit,
        }
        if decimals is not None:
            defaults["decimals"] = decimals
        if minimum is not None:
            defaults["min"] = minimum
        self.panels.append({
            "datasource": DS, "description": description, "fieldConfig": {"defaults": defaults, "overrides": []},
            "gridPos": self._place(w, h), "id": self._next_id(),
            "options": {"colorMode": color_mode if steps or color_mode == "none" else "value", "graphMode": "area" if graph else "none",
                        "justifyMode": "center", "orientation": "auto", "percentChangeColorMode": "standard",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showPercentChange": False, "textMode": text_mode, "wideLayout": True},
            "targets": targets, "title": title, "type": "stat",
        })

    def series(self, title: str, targets: list[dict], *, w: int = 12, h: int = 8, unit: str = "short", description: str = "",
               overrides: list | None = None, stack: bool = False, fill: int = 8, minimum: float | None = None, maximum: float | None = None,
               soft_max: float | None = None, soft_min: float | None = None, steps=None, threshold_lines: bool = False,
               interpolation: str = "linear", decimals: int | None = None) -> None:
        custom: dict[str, Any] = {
            "axisBorderShow": False, "axisCenteredZero": False, "axisColorMode": "text", "axisLabel": "", "axisPlacement": "auto",
            "barAlignment": 0, "drawStyle": "line", "fillOpacity": 30 if stack else fill, "gradientMode": "none",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "insertNulls": False, "lineInterpolation": interpolation,
            "lineWidth": 2, "pointSize": 4, "scaleDistribution": {"type": "linear"}, "showPoints": "never", "spanNulls": False,
            "stacking": {"group": "A", "mode": "normal" if stack else "none"},
            "thresholdsStyle": {"mode": "dashed" if threshold_lines else "off"},
        }
        if soft_max is not None:
            custom["axisSoftMax"] = soft_max
        if soft_min is not None:
            custom["axisSoftMin"] = soft_min
        defaults: dict[str, Any] = {"color": {"mode": "palette-classic"}, "custom": custom, "mappings": [],
                                    "thresholds": _steps(steps or [(GOOD, None)]), "unit": unit}
        for k, v in (("min", minimum), ("max", maximum), ("decimals", decimals)):
            if v is not None:
                defaults[k] = v
        self.panels.append({
            "datasource": DS, "description": description, "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "gridPos": self._place(w, h), "id": self._next_id(),
            "options": {"legend": {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
            "targets": targets, "title": title, "type": "timeseries",
        })

    def bars(self, title: str, targets: list[dict], *, w: int = 12, h: int = 7, unit: str = "percentunit", steps=None, description: str = "",
             minimum: float = 0, maximum: float = 1) -> None:
        self.panels.append({
            "datasource": DS, "description": description,
            "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "mappings": [], "max": maximum, "min": minimum,
                                         "noValue": "no data", "thresholds": _steps(steps or [(BLUE, None)]), "unit": unit}, "overrides": []},
            "gridPos": self._place(w, h), "id": self._next_id(),
            "options": {"displayMode": "gradient", "legend": {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": False},
                        "maxVizHeight": 300, "minVizHeight": 16, "minVizWidth": 8, "namePlacement": "auto", "orientation": "horizontal",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showUnfilled": True,
                        "sizing": "auto", "valueMode": "color"},
            "targets": targets, "title": title, "type": "bargauge",
        })

    def timeline(self, title: str, targets: list[dict], *, w: int = 24, h: int = 5, description: str = "", overrides: list | None = None) -> None:
        self.panels.append({
            "datasource": DS, "description": description,
            "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "custom": {"fillOpacity": 80, "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                                                                                       "insertNulls": False, "lineWidth": 0, "spanNulls": False},
                                         "mappings": [{"options": {"1": {"index": 0, "text": "on"}}, "type": "value"}],
                                         "noValue": "no burn-rate alert has been pending or firing in this time range",
                                         "thresholds": _steps([(CRIT, None)])}, "overrides": overrides or []},
            "gridPos": self._place(w, h), "id": self._next_id(),
            "options": {"alignValue": "left", "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                        "mergeValues": True, "rowHeight": 0.8, "showValue": "never", "tooltip": {"mode": "single", "sort": "none"}},
            "targets": targets, "title": title, "type": "state-timeline",
        })

    def text(self, title: str, markdown: str, *, w: int = 24, h: int = 6) -> None:
        self.panels.append({"gridPos": self._place(w, h), "id": self._next_id(),
                            "options": {"code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False},
                                        "content": markdown, "mode": "markdown"},
                            "title": title, "type": "text"})

    def build(self) -> dict:
        return {
            "annotations": {"list": [
                {"builtIn": 1, "datasource": GRAFANA_DS, "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                 "name": "Annotations & Alerts", "type": "dashboard"},
                {"datasource": GRAFANA_DS, "enable": True, "iconColor": CRIT, "name": "Injected faults (posted by the harness)",
                 "target": {"limit": 100, "matchAny": False, "tags": [TAG, "fault"], "type": "tags"}},
                {"datasource": GRAFANA_DS, "enable": True, "iconColor": VIOLET, "name": "Experiment runs (posted by the harness)",
                 "target": {"limit": 100, "matchAny": False, "tags": [TAG, "run"], "type": "tags"}},
                {"datasource": DS, "enable": True, "iconColor": WARN, "name": "SLO burn-rate alerts firing", "expr": 'ALERTS{alertname=~"SLOBurn.*",alertstate="firing"}',
                 "step": "10s", "titleFormat": "{{alertname}}", "textFormat": "severity={{severity}}", "useValueForTime": False},
            ]},
            "description": self.description, "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1, "id": None,
            "links": [{"asDropdown": False, "icon": "external link", "includeVars": True, "keepTime": True, "tags": [TAG], "targetBlank": False,
                       "title": "SLO-Loop dashboards", "tooltip": "", "type": "dashboards", "url": ""}],
            "liveNow": False, "panels": self.panels, "refresh": self.refresh, "schemaVersion": 39, "tags": self.tags,
            "templating": {"list": [{"current": {}, "hide": 2, "includeAll": False, "label": "Data source", "multi": False, "name": "datasource",
                                     "options": [], "query": "prometheus", "queryValue": "", "refresh": 1, "regex": "", "skipUrlSync": False,
                                     "type": "datasource"}]},
            "time": {"from": self.range_from, "to": "now"},
            "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]}, "timezone": "browser",
            "title": self.title, "uid": self.uid, "version": 1, "weekStart": "",
        }


def _class_overrides(prefix: str = "", *, dashed_prefix: str | None = None) -> list[dict]:
    out = []
    for c, col in CLASS_COLOR.items():
        out.append(_override("byRegexp", f"/^{prefix}{c}$/", {"color": fixed(col)}))
        if dashed_prefix:
            out.append(_override("byRegexp", f"/^{dashed_prefix}{c}$/", {"color": fixed(col), "custom.lineStyle": DASHED, "custom.fillOpacity": 0, "custom.lineWidth": 1}))
    return out


# ----------------------------------------------------------------------------------------------- the three dashboards
def health() -> dict:
    b = Board("slo-loop-health", "SLO-Loop · Service health",
              "Is the LLM service meeting its SLO, and how much error budget is left? Start here. Source: github.com/suhasgowtham-tech/slo-loop-lab",
              [TAG, "health"], refresh="5s", range_from="now-15m")
    b.stat("Error budget left", [_target("A", BUDGET_LEFT, "budget left", instant=True)], unit="percentunit", decimals=0, steps=BUDGET_STEPS,
           description="Share of the error budget not yet spent over the selected time range. 100 % = no bad request, 0 % = the SLO was met exactly, "
                       "below 0 = the SLO was missed. Budget = (1 - SLO target) x requests.")
    b.stat("SLO compliance", [_target("A", RANGE_GOOD, "compliance", instant=True)], unit="percentunit", decimals=2, color_mode="none",
           description="Requests that finished OK and produced the first token within their class threshold, over the selected time range. The target is the red line on the chart below.")
    b.stat("Burn rate (2 min)", [_target("A", "slo:burn_rate:request_120s", "burn", instant=True)], unit="suffix:x", decimals=1, steps=BURN_STEPS,
           description="How fast the error budget is being spent right now. 1x = exactly on budget. The bands match the alerts: 2.5x opens a ticket, 6x pages.")
    b.stat("Alerts firing", [_target("A", 'count(ALERTS{alertname=~"SLOBurn.*",alertstate="firing"}) or vector(0)', "firing", instant=True)],
           unit="short", decimals=0, steps=[(GOOD, None), (CRIT, 1)], description="SLO burn-rate alerts currently firing (SLOBurnFast, SLOBurnSlow, SLOBurnFastToken).")
    b.stat("Throughput", [_target("A", f"sum(rate({TOTAL}[1m]))", "req/s")], unit="reqps", decimals=2, color_mode="none", graph=True,
           description="Completed requests per second, all outcomes.")
    b.stat("LLM replicas ready", [_target("A", READY, "ready")], unit="short", decimals=0, color_mode="none", graph=True,
           description="llama.cpp pods that are ready to serve (each one loads the model before it counts).")

    b.row("Service level")
    b.series("Good-request ratio vs SLO target", [
        _target("A", GOOD_RATIO_120S, "all classes"),
        _target("B", "slo:good_ratio:class_120s", "{{class}}"),
        _target("C", "slo:target_ratio", "SLO target"),
    ], unit="percentunit", maximum=1, soft_min=0.9, decimals=1, fill=0,
        description="Share of requests that were good over the last 2 minutes, in total and per prompt class. The dashed red line is the SLO target from config/slo.yaml; below it, budget is being spent.",
        overrides=_class_overrides() + [
            _override("byName", "all classes", {"color": fixed("text"), "custom.lineWidth": 3}),
            _override("byName", "SLO target", {"color": fixed(CRIT), "custom.lineStyle": DASHED, "custom.lineWidth": 1}),
        ])
    b.series("Error-budget burn rate", [
        _target("A", "slo:burn_rate:request_30s", "request SLI, 30 s"),
        _target("B", "slo:burn_rate:request_120s", "request SLI, 2 min"),
        _target("C", "slo:burn_rate:token_120s", "token-weighted SLI, 2 min"),
    ], unit="suffix:x", minimum=0, soft_max=8, decimals=1, steps=[(GOOD, None), (SERIOUS, 2.5), (CRIT, 6)], threshold_lines=True, fill=0,
        description="1x = spending the budget exactly as fast as it accrues. Dashed lines are the alert thresholds: 2.5x (ticket) and 6x (page). The token-weighted line counts a long request more than a short one.",
        overrides=[_override("byName", "request SLI, 30 s", {"color": fixed(BLUE)}),
                   _override("byName", "request SLI, 2 min", {"color": fixed(BLUE_LIGHT)}),
                   _override("byName", "token-weighted SLI, 2 min", {"color": fixed(ORANGE)})])
    b.series("Request outcomes (req/s)", [
        _target("A", f"sum(rate({GOODC}[30s]))", "good"),
        _target("B", f'clamp_min(sum(rate({TOTAL}{{status="ok"}}[30s])) - sum(rate({GOODC}[30s])), 0)', "slow (missed the TTFT threshold)"),
        _target("C", f'sum(rate({TOTAL}{{status!="ok"}}[30s])) or vector(0)', "failed"),
    ], unit="reqps", stack=True, decimals=2,
        description="Every completed request is one of: good (OK and fast enough), slow (OK but over its TTFT threshold) or failed (timeout or error). Stacked, so the height is total throughput.",
        overrides=[_override("byName", "good", {"color": fixed(GOOD)}),
                   _override("byName", "slow (missed the TTFT threshold)", {"color": fixed(WARN)}),
                   _override("byName", "failed", {"color": fixed(CRIT)})])
    b.series("Time to first token (p95) vs SLO threshold", [
        _target("A", "histogram_quantile(0.95, sum by (le, class) (rate(slo_ttft_seconds_bucket[1m])))", "p95 {{class}}"),
        _target("B", "slo:ttft_threshold_seconds", "SLO {{class}}"),
    ], unit="s", minimum=0, decimals=1, fill=0,
        description="Solid = measured p95 TTFT per prompt class. Dashed = the threshold that class must stay under (calibrated on this machine, config/slo.yaml). A solid line above its dashed line means that class is missing its SLO.",
        overrides=_class_overrides("p95 ", dashed_prefix="SLO "))

    b.row("Budget and capacity")
    b.bars("Error budget left, by prompt class", [_target("A", BUDGET_LEFT_BY_CLASS, "{{class}}", instant=True)], steps=BUDGET_STEPS, minimum=0, maximum=1,
           description="The same number as the headline tile, split by prompt class over the selected time range: which class is spending the budget? A bar at 0 with a negative value means that class missed its SLO.")
    b.series("Load vs capacity", [
        _target("A", "sum(slo_inflight)", "in flight (admitted, not finished)"),
        _target("B", "slo:llm_slots", "LLM slots (ready replicas x parallel)"),
    ], unit="short", minimum=0, decimals=0, fill=15, interpolation="stepAfter",
        description="When in-flight requests exceed the LLM's concurrent slots, the extra requests wait inside llama.cpp, and time to first token grows.",
        overrides=[_override("byName", "in flight (admitted, not finished)", {"color": fixed(BLUE)}),
                   _override("byName", "LLM slots (ready replicas x parallel)", {"color": fixed("text"), "custom.fillOpacity": 0, "custom.lineStyle": DASHED, "custom.lineWidth": 1})])

    b.row("How to read this page")
    b.text("Reading guide", (
        "**SLO** - a promise about user experience: at least the target share of requests (99 % by default) must succeed *and* start answering within a per-class time (TTFT).\n\n"
        "**Error budget** - the share of requests that are allowed to break the promise. *Budget left* shows how much of it the selected time range has not yet used.\n\n"
        "**Burn rate** - how fast the budget is being used: 1x is exactly on budget, 6x would empty a budget in one sixth of its period. That is why the pager fires at 6x and a ticket opens at 2.5x.\n\n"
        "**Markers** - red = a fault the harness injected (with its name on hover), purple = an experiment run, yellow = a burn-rate alert firing.\n\n"
        "Next: **Incident view** to see what broke and where, **Autoscaling experiment** to see how capacity followed the load."), h=7)
    return b.build()


def incident() -> dict:
    b = Board("slo-loop-incident", "SLO-Loop · Incident view",
              "What broke, where and since when? Burn rate, alert timeline, stage latency, failure causes and saturation, with fault markers. Source: github.com/suhasgowtham-tech/slo-loop-lab",
              [TAG, "incident"], refresh="5s", range_from="now-15m")
    b.stat("Fast burn (30 s)", [_target("A", "slo:burn_rate:request_30s", "burn", instant=True)], unit="suffix:x", decimals=1, steps=BURN_STEPS,
           description="Budget burn rate over 30 seconds. Pages at 6x (with the 2-minute rate also at 6x).")
    b.stat("Slow burn (5 min)", [_target("A", "slo:burn_rate:request_300s", "burn", instant=True)], unit="suffix:x", decimals=1, steps=BURN_STEPS,
           description="Budget burn rate over 5 minutes. Opens a ticket at 2.5x.")
    b.stat("Failed requests/s", [_target("A", f'sum(rate({TOTAL}{{status!="ok"}}[30s])) or vector(0)', "failed", instant=True)],
           unit="reqps", decimals=2, steps=[(GOOD, None), (CRIT, 0.01)], description="Requests that ended in a timeout or an error (not merely slow).")
    b.stat("Retriever p95", [_target("A", "histogram_quantile(0.95, sum by (le) (rate(retriever_duration_seconds_bucket[1m])))", "retriever p95", instant=True)],
           unit="s", decimals=2, steps=[(GOOD, None), (SERIOUS, 0.25), (CRIT, 1)], description="95th-percentile retrieval time, including any injected fault latency.")
    b.stat("LLM first token p95 (short)", [_target("A", 'histogram_quantile(0.95, sum by (le) (rate(slo_ttft_seconds_bucket{class="short"}[1m])))', "ttft p95", instant=True)],
           unit="s", decimals=2, color_mode="none", description="95th-percentile time to first token for the short prompt class.")
    b.stat("Queue depth", [_target("A", QUEUE_DEPTH, "queued", instant=True)], unit="short", decimals=0, steps=[(GOOD, None), (WARN, 1), (SERIOUS, 3)],
           description="In-flight requests beyond the LLM's concurrent slots: requests that are waiting rather than being served.")

    b.row("What fired, and when")
    b.timeline("Burn-rate alerts", [_target("A", 'ALERTS{alertname=~"SLOBurn.*",alertstate=~"pending|firing"}', "{{alertname}} ({{alertstate}})")],
               description="One lane per alert and state. Pending = the condition is true but the `for:` clause has not elapsed yet; firing = it would page or ticket.",
               overrides=[_override("byRegexp", "/.*\\(pending\\)/", {"color": fixed(WARN)}), _override("byRegexp", "/.*\\(firing\\)/", {"color": fixed(CRIT)})])

    b.row("Which part is slow?")
    b.series("Stage latency (p95)", [_target("A", "histogram_quantile(0.95, sum by (le, stage) (rate(slo_stage_seconds_bucket[1m])))", "{{stage}}")],
             unit="s", minimum=0, decimals=1, fill=0,
             description="Time spent in each stage of a request. `retrieve` is the retriever; `llm_ttft` is the wait for the model's first token. The stage that moved is the stage to look at. Compare with the dashed threshold on the Service health page.",
             overrides=[_override("byName", s, {"color": fixed(c)}) for s, c in STAGE_COLOR.items()])
    b.series("Failures by cause (req/s)", [_target("A", f'sum by (status) (rate({TOTAL}{{status!="ok"}}[30s]))', "{{status}}")], unit="reqps", decimals=2, stack=True,
             description="Failed requests split by how they failed (timeout, llm_error, llm_http_*, llm_incomplete). Empty is good.")

    b.row("Which class is burning the budget?")
    b.series("Burn rate by prompt class (2 min)", [_target("A", BURN_BY_CLASS, "{{class}}")], unit="suffix:x", minimum=0, soft_max=8, decimals=1, fill=0,
             steps=[(GOOD, None), (SERIOUS, 2.5), (CRIT, 6)], threshold_lines=True, overrides=_class_overrides(),
             description="Burn rate computed separately for each prompt class. A fault that only hurts long prompts shows up here before it moves the overall number.")
    b.series("Request vs token-weighted burn (2 min)", [
        _target("A", "slo:burn_rate:request_120s", "request SLI"),
        _target("B", "slo:burn_rate:token_120s", "token-weighted SLI"),
    ], unit="suffix:x", minimum=0, soft_max=8, decimals=1, fill=0, steps=[(GOOD, None), (SERIOUS, 2.5), (CRIT, 6)], threshold_lines=True,
        description="The request SLI counts every request equally; the token-weighted SLI counts a long request more. When they diverge, the mix of prompt sizes changed, not only the speed.",
        overrides=[_override("byName", "request SLI", {"color": fixed(BLUE)}), _override("byName", "token-weighted SLI", {"color": fixed(ORANGE)})])

    b.row("Is it capacity?")
    b.series("Queue depth", [_target("A", QUEUE_DEPTH, "queued requests")], w=8, unit="short", minimum=0, decimals=0, fill=15,
             description="Requests waiting for an LLM slot. Persistently above 0 means the LLM, not the retriever, is the bottleneck.",
             overrides=[_override("byName", "queued requests", {"color": fixed(SERIOUS)})])
    b.series("LLM CPU (cores) vs request", [
        _target("A", f"sum(rate(container_cpu_usage_seconds_total{{{LLM_POD}}}[1m]))", "used"),
        _target("B", f'sum(kube_pod_container_resource_requests{{{LLM_POD},resource="cpu"}})', "requested"),
    ], w=8, unit="short", minimum=0, decimals=2, fill=15,
        description="CPU cores the LLM pods use against what they asked for. A CPU stress fault pushes `used` to the limit; llama.cpp is CPU-bound here.",
        overrides=[_override("byName", "used", {"color": fixed(BLUE)}),
                   _override("byName", "requested", {"color": fixed("text"), "custom.fillOpacity": 0, "custom.lineStyle": DASHED, "custom.lineWidth": 1})])
    b.series("LLM replicas: desired vs ready", [_target("A", DESIRED, "desired"), _target("B", READY, "ready")], w=8, unit="short", minimum=0, decimals=0,
             fill=0, interpolation="stepAfter",
             description="A pod kill shows as `ready` dipping below `desired`; a new replica needs time to load the model before it is ready.",
             overrides=[_override("byName", "desired", {"color": fixed("text"), "custom.lineStyle": DASHED, "custom.lineWidth": 1}),
                        _override("byName", "ready", {"color": fixed(BLUE)})])
    return b.build()


def autoscaling() -> dict:
    b = Board("slo-loop-autoscaling", "SLO-Loop · Autoscaling experiment",
              "Does LLM capacity follow demand, which signal drives it, and what does it cost? KEDA policies cpu / queue / burn vs static replicas. Source: github.com/suhasgowtham-tech/slo-loop-lab",
              [TAG, "autoscaling"], refresh="10s", range_from="now-30m")
    b.stat("Active policy", [_target("A", "slo:autoscaling_policy_info", "{{policy}}", instant=True)], unit="short", color_mode="none", text_mode="name",
           description="The autoscaling policy in the running Helm release: static1/2/3 (fixed replicas) or a KEDA policy (cpu, queue, burn).")
    b.stat("LLM replicas ready", [_target("A", READY, "ready")], unit="short", decimals=0, color_mode="none", graph=True,
           description="llama.cpp pods that are ready to serve (each one loads the model before it counts).")
    b.stat("Replicas starting", [_target("A", f"{DESIRED} - {READY}", "starting", instant=True)], unit="short", decimals=0, steps=[(GOOD, None), (WARN, 1)],
           description="Desired minus ready: replicas the autoscaler asked for that are not serving yet (the model is still loading).")
    b.stat("Replica-minutes used", [_target("A", f"sum(avg_over_time(kube_deployment_status_replicas_ready{{{LLM}}}[$__range])) * $__range_s / 60", "replica-min", instant=True)],
           unit="short", decimals=1, color_mode="none",
           description="Average ready replicas x length of the selected time range, in minutes: what this policy cost. Compare with the budget tile.")
    b.stat("Error budget left", [_target("A", BUDGET_LEFT, "budget left", instant=True)], unit="percentunit", decimals=0, steps=BUDGET_STEPS,
           description="What the replica-minutes bought: share of the error budget not yet spent in the selected time range.")
    b.stat("Throughput per replica", [_target("A", f"sum(rate({TOTAL}[1m])) / (sum(kube_deployment_status_replicas_ready{{{LLM}}}) > 0)", "req/s/replica")],
           unit="reqps", decimals=2, color_mode="none", graph=True, description="Completed requests per second per ready LLM replica.")

    b.row("Does capacity follow demand?")
    b.series("LLM replicas: desired vs ready", [_target("A", DESIRED, "desired"), _target("B", READY, "ready")], unit="short", minimum=0, decimals=0, fill=0,
             interpolation="stepAfter",
             description="The gap between the lines is the cold start: a new llama.cpp replica is not ready until it has loaded the model. That lag is why reactive autoscaling struggles with bursts.",
             overrides=[_override("byName", "desired", {"color": fixed("text"), "custom.lineStyle": DASHED, "custom.lineWidth": 1}),
                        _override("byName", "ready", {"color": fixed(BLUE)})])
    b.series("Load vs capacity", [
        _target("A", "sum(slo_inflight)", "in flight (admitted, not finished)"),
        _target("B", "slo:llm_slots", "LLM slots (ready replicas x parallel)"),
    ], unit="short", minimum=0, decimals=0, fill=15, interpolation="stepAfter",
        description="Demand (in-flight requests) against serving capacity (ready replicas x concurrent slots per replica). Demand above the dashed line queues.",
        overrides=[_override("byName", "in flight (admitted, not finished)", {"color": fixed(BLUE)}),
                   _override("byName", "LLM slots (ready replicas x parallel)", {"color": fixed("text"), "custom.fillOpacity": 0, "custom.lineStyle": DASHED, "custom.lineWidth": 1})])

    b.row("The three scaling signals, each against its trigger threshold")
    only_active = " Only the active policy acts on this signal; the others are shown so you can see what each policy would have reacted to."
    b.series("CPU policy: utilisation vs target", [
        _target("A", f'sum(rate(container_cpu_usage_seconds_total{{{LLM_POD}}}[1m])) / sum(kube_pod_container_resource_requests{{{LLM_POD},resource="cpu"}})', "CPU utilisation (of request)"),
        _target("B", 'slo:scaling_threshold{signal="cpu"}', "trigger target"),
    ], w=8, unit="percentunit", minimum=0, decimals=0, fill=10,
        description="LLM CPU as a share of what the pods requested. The KEDA cpu trigger scales out when this stays above the target." + only_active,
        overrides=[_override("byName", "CPU utilisation (of request)", {"color": fixed(BLUE)}),
                   _override("byName", "trigger target", {"color": fixed(CRIT), "custom.lineStyle": DASHED, "custom.lineWidth": 1, "custom.fillOpacity": 0})])
    b.series("Queue policy: in-flight per replica vs target", [
        _target("A", f"sum(slo_inflight) / (sum(kube_deployment_status_replicas_ready{{{LLM}}}) > 0)", "in flight per replica"),
        _target("B", 'slo:scaling_threshold{signal="queue"}', "trigger target"),
    ], w=8, unit="short", minimum=0, decimals=1, fill=10,
        description="Backlog per replica. The KEDA queue trigger scales out when it exceeds the target (the number of concurrent slots per replica)." + only_active,
        overrides=[_override("byName", "in flight per replica", {"color": fixed(BLUE)}),
                   _override("byName", "trigger target", {"color": fixed(CRIT), "custom.lineStyle": DASHED, "custom.lineWidth": 1, "custom.fillOpacity": 0})])
    b.series("Burn policy: 30 s burn rate vs target", [
        _target("A", "max(slo:burn_rate:request_30s)", "burn rate (30 s)"),
        _target("B", 'slo:scaling_threshold{signal="burn"}', "trigger target"),
    ], w=8, unit="suffix:x", minimum=0, soft_max=8, decimals=1, fill=10,
        description="Short-window error-budget burn. The KEDA burn trigger scales replicas in proportion to burn / target." + only_active,
        overrides=[_override("byName", "burn rate (30 s)", {"color": fixed(BLUE)}),
                   _override("byName", "trigger target", {"color": fixed(CRIT), "custom.lineStyle": DASHED, "custom.lineWidth": 1, "custom.fillOpacity": 0})])

    b.row("What it bought")
    b.series("Good-request ratio vs SLO target", [
        _target("A", GOOD_RATIO_120S, "all classes"),
        _target("B", "slo:target_ratio", "SLO target"),
    ], unit="percentunit", maximum=1, soft_min=0.9, decimals=1, fill=0,
        description="Did the capacity the policy chose keep requests inside the SLO?",
        overrides=[_override("byName", "all classes", {"color": fixed("text"), "custom.lineWidth": 3}),
                   _override("byName", "SLO target", {"color": fixed(CRIT), "custom.lineStyle": DASHED, "custom.lineWidth": 1})])
    b.series("Time to first token (p95) vs SLO threshold", [
        _target("A", "histogram_quantile(0.95, sum by (le, class) (rate(slo_ttft_seconds_bucket[1m])))", "p95 {{class}}"),
        _target("B", "slo:ttft_threshold_seconds", "SLO {{class}}"),
    ], unit="s", minimum=0, decimals=1, fill=0,
        description="Solid = measured p95 TTFT per prompt class; dashed = the threshold each class must stay under.",
        overrides=_class_overrides("p95 ", dashed_prefix="SLO "))
    return b.build()


DASHBOARDS = {
    "slo-loop-health.json": health,
    "slo-loop-incident.json": incident,
    "slo-loop-autoscaling.json": autoscaling,
}


def render(builder) -> str:
    return json.dumps(builder(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m lab.dashgen", description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="do not write; exit 1 if a committed dashboard differs from the generated one")
    a = p.parse_args(argv)
    stale = []
    for name, builder in DASHBOARDS.items():
        text, path = render(builder), OUT_DIR / name
        if a.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if stale:
        print("out of date (run `make dashboards-build`): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
