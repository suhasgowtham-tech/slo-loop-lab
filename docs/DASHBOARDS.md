# Dashboards

Three Grafana dashboards ship with the Helm chart, so `make deploy` loads them and nothing is clicked together by hand. They are written as code (`lab/dashgen.py`), and their queries are tested against a real Prometheus engine.

```bash
make dashboards          # where are they, are they loaded, does Prometheus have what they draw?  (read-only, ~2 s)
make demo                # one guided incident, alert to recovery, for a screen recording  (4.5 min + about 2 min setup)
```

Grafana runs on `127.0.0.1:3000` only (login `admin` / `slo-loop-lab`, local lab password). Nothing is exposed to your network.

## The three dashboards

| Dashboard | Question it answers | For whom |
|---|---|---|
| **Service health** (`/d/slo-loop-health`) | Are we meeting the SLO, and how much error budget is left? | VPs, founders, anyone who needs the answer in 10 seconds |
| **Incident view** (`/d/slo-loop-incident`) | What broke, where, and since when? | the on-call engineer |
| **Autoscaling experiment** (`/d/slo-loop-autoscaling`) | Does capacity follow demand, which signal drives it, and what does it cost? | the lab's own experiment (Phase B) |

**Service health**: six tiles (error budget left, SLO compliance, burn rate, alerts firing, throughput, ready replicas), then good-request ratio against the SLO target, burn rate with the two alert thresholds drawn as lines, request outcomes (good / slow / failed, stacked), time-to-first-token p95 per class against that class's threshold, error budget left per prompt class, load against LLM slots, and a short reading guide for people who do not live in SLO language.

**Incident view**: fast and slow burn, failed requests, retriever p95, LLM first-token p95 and queue depth as tiles; an alert timeline (pending vs firing); stage latency (retriever vs LLM first token) to see *which stage moved*; failures by cause; burn rate *per prompt class*; request-SLI vs token-weighted burn; and the capacity checks (queue depth, LLM CPU against its request, desired vs ready replicas).

**Autoscaling experiment**: active policy, replicas ready, replicas still starting, replica-minutes used, error budget left (what the replicas bought), and throughput per replica; replicas desired vs ready (the gap is the model cold start); load vs capacity; and the three KEDA signals (CPU utilisation, in-flight per replica, 30 s burn rate), each against its trigger threshold, so you can see what each policy *would have* reacted to.

## Markers: faults, runs and alerts on every time series

| Marker | Colour | Source |
|---|---|---|
| Injected fault (a band from apply to revert, named on hover) | red | the harness posts it to Grafana when a fault is *actually applied*, `lab/annotate.py` |
| Experiment run (a band for the measured window) | purple | the harness |
| SLO burn-rate alert firing | yellow | Prometheus' `ALERTS` series, no harness involved |

Add your own while recording: `.venv/bin/slo-lab annotate "scaled to 3 replicas"`.

**Markers can never disturb a run.** They are posted from one background thread with a 2 s timeout, after the fault has been applied; fault times come from the absolute schedule, not from how long Grafana takes; two failures in a row switch markers off for the run; `LAB_ANNOTATE=0` switches them off entirely. `tests/test_annotate.py` includes a test with a dead Grafana that checks the faults still fire on schedule.

## Nothing is retyped: thresholds come from the same place as the alerts

A dashboard that hard-codes "99 %" or "10.4 s" goes stale the moment you run `make calibrate`. These panels draw their lines from recording rules the chart defines from the same values the alerts and KEDA use:

| Recording rule | From | Drawn as |
|---|---|---|
| `slo:target_ratio` | `slo.target` | SLO target line; divisor of every budget number |
| `slo:ttft_threshold_seconds{class}` | `slo.ttft_s` | the dashed threshold each class must stay under |
| `slo:llm_slots` | ready replicas x `llm.parallel` | capacity line |
| `slo:scaling_threshold{signal}` | `autoscaling.{cpu,queue,burn}` | KEDA trigger thresholds |
| `slo:autoscaling_policy_info{policy}` | `autoscaling.policy` | the "Active policy" tile |

The 2.5x and 6x bands on the burn-rate panels equal the alert thresholds; a test reads both files and fails if they differ.

## How to read the numbers (and what they are not)

* **Error budget left** is computed over the time range selected in Grafana (last 15 minutes by default), not over a 30-day window. 100 % = no bad request, 0 % = the SLO was met exactly, below 0 = missed. For a rolling 30-day budget you would use a 30-day range against long-retention data, which this laptop lab does not keep (Prometheus retention is 2 days).
* **Burn rate** uses the lab's compressed windows (30 s, 2 min, 5 min), because the lab's scenarios last 10 minutes. The measured detector comparison is the offline replay in `lab/analysis/detect.py`; the alerts on these dashboards are demo alerts.
* **Token-weighted SLI** counts a long request more than a short one (a proxy for compute cost). It is a proxy, not billing data.
* **Slow** means "finished OK but over its class's TTFT threshold"; **failed** means timeout or error. A request is *good* only when it is neither.
* On the Autoscaling dashboard, only the **active** policy acts on its signal; the other two panels show what the other policies would have seen, not what happened.

## The demo (`make demo`)

One retriever fault (the same fault as scenario s5, shortened to 60 s), phase X, so nothing enters the experiment results. It refuses to start if the dashboards are not loaded. The command prints the three URLs and the timeline to expect. A reasonable recording is about 90 seconds of the Service health and Incident view tabs from just before the fault to the first recovery; the alert resolves about two minutes after the fault stops, because the 2-minute window must empty first.

Screenshots worth taking: Service health (budget tile and burn rate during the fault), Incident view (alert timeline and stage latency: the retriever stage moves, the LLM stage does not), and Autoscaling with the `cpu` policy. Save them in `docs/img/`.

## Dashboards as code

```bash
make dashboards-build    # regenerate charts/slo-loop-app/dashboards/*.json from lab/dashgen.py
```

Edit `lab/dashgen.py`, never the JSON. `tests/test_dashboards.py` fails when the committed JSON differs from the generator, when a panel has no description, when two panels overlap, when a class changes colour between panels, or when a query hard-codes a threshold. `tests/test_dashboard_queries.py` evaluates every panel query with Prometheus' own engine (`promtool test rules`) over synthetic counters whose totals are known by construction, and fails if any query returns a different number than the one worked out by hand, or if a query has no such expectation. It needs `promtool` (downloaded in CI; skipped locally if absent).

What is **not** covered by an automated test: how the panels *look* in Grafana. Layout, colours and legend wrapping are checked by eye.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `make dashboards` says dashboards or recording rules are missing | The cluster still runs a chart from before these dashboards existed. `make deploy && make verify` (about 3 minutes; it resets the autoscaling policy to `static1`). Wait 30 s after deploying: the Grafana sidecar loads dashboards on a short delay. |
| Panels say *no data* | The lab is idle: rates need traffic. Run `make demo` or any scenario. A tile that shows *no data* with traffic running usually means a recording rule is missing: `make dashboards`. |
| No red bands on the charts during a run | Markers are best effort: check `LAB_ANNOTATE` is not `0`, that `make dashboards` can log in, and that the run printed no `[annotate] Grafana markers are off` line. The run itself is unaffected either way. |
| An old *SLO-Loop Lab* dashboard is still listed | The pre-v2.8 dashboard. It is removed when its ConfigMap is; if Grafana keeps it, delete it in the UI. |
