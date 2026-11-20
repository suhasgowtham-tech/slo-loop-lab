# Design, methodology and threats to validity

This document is the part to read before an interview. Each choice below says what it is, why, and what it costs.

## 1. Research question

Classic SRE practice (SLOs, error budgets, multi-window multi-burn-rate alerting, utilisation-based autoscaling)
was shaped by request/response services whose requests cost roughly the same. LLM serving breaks that assumption:

- **Cost varies by orders of magnitude per request** (prompt length × output length).
- **Latency is heavy-tailed and load-dependent** (requests queue behind a fixed number of decode slots).
- **"Slow" has two meanings**: time-to-first-token (TTFT) and per-token speed.
- **CPU/GPU utilisation is a poor load signal** (a saturated accelerator can sit next to a calm CPU; with batching
  the accelerator is "busy" at almost any load).

The lab measures *where and by how much* the classic methods degrade, rather than building another AIOps tool.

## 2. Service under test

`load generator → gateway → retriever → llama.cpp`. The gateway is the single place SLIs are defined, so Prometheus,
the dashboard and the offline analysis all agree.

### SLI definitions (must match `apps/gateway/app.py`, `lab/loadgen.py`, `lab/analysis/slo.py`)

| Term | Definition |
|---|---|
| prompt class | `short` (≈50 words), `medium` (≈400), `long` (≈1200); each asks for 64 output tokens |
| TTFT | time from request receipt to the first chunk from llama.cpp (includes retrieval, queueing inside llama.cpp and prompt evaluation). The offline analysis uses the **client-observed** first token, Prometheus the gateway-observed one; they differ by milliseconds |
| good event | status `ok` **and** TTFT ≤ the class threshold. Timeouts, retriever failures, LLM errors and client aborts are bad |
| weight | `ceil(len(prompt)/4) + max_tokens`, a cost proxy known at admission time (tested equal in gateway and load generator) |
| request SLI | good events / events |
| token-weighted SLI | Σ weight(good) / Σ weight(all) |
| class SLI | request SLI per prompt class; the burn rate is the **worst** class |
| burn rate | (bad fraction in the window) / (1 − target). Target 0.99, so burn 1 consumes the budget exactly over the SLO period |

TTFT thresholds are **derived from your machine** (`make calibrate`: 1.5 × the p99 TTFT of a clean 15-minute baseline,
per class) so the lab is meaningful on any laptop. Capacity is probed (`make capacity`) and `base_rps = 0.75 × capacity`,
so one replica is comfortable at baseline and a 3× burst is a real overload.

### Why these llama.cpp settings
`cache_prompt=false` (otherwise repeated prefixes are served from cache and TTFT is distorted), `ignore_eos=true`
(fixed output length ⇒ lower variance and a controlled cost per class), 2 parallel slots per replica (requests beyond that
queue inside llama.cpp, the behaviour under study), `--metrics` enabled for observability only: the lab does not depend on it.

## 3. Load generation: open loop, on purpose

Arrivals follow a pre-computed piecewise-constant-rate Poisson schedule and are sent at their scheduled time regardless of
outstanding requests. A closed-loop client (N workers waiting for replies) slows down exactly when the system slows down
and hides queueing delay ("coordinated omission"). `tests/test_e2e_local.py::test_open_loop_exposes_queueing_that_closed_loop_would_hide`
demonstrates the difference. The generator records its own maximum scheduling lag; above 0.25 s the **run is marked invalid**
and excluded from analysis. Per-request seeds make every run deterministic given `(scenario, rep)`.

### Workload profiles and why the default is "lite"

On CPU llama.cpp the cost of a request is dominated by prompt evaluation (about 14 ms per prompt token and 40 ms per generated
token on a 10-thread laptop i7 with 2 threads per replica, measured from the server's own log lines). The first
(`standard`) profile costs about 8 s per request there, so ONE replica serves ~0.12 req/s and a 10-minute run holds ~35
requests: a 99 % SLO cannot even be evaluated on 35 events. `lite` keeps the shape (three classes, a >= 8x cost spread, a
long-prompt tail) at about a third of the cost. The retriever contributes ~8 prompt tokens (one snippet of six real words)
instead of ~64 (digits tokenise one by one), and the idle per-class latency is measured, not assumed (`make latency`).
Capacity probing starts at 35 % of the rate that measurement predicts and retreats downward if even that is too much.

## 4. Detection methodology (H1, H2)

Detectors are **replayed offline** on the per-request log, so any number of detector configurations is compared on
**identical traffic** (no run-to-run noise between detectors). Events are timestamped at **completion**: an alerting system
can only react to outcomes it has observed, so a request that hangs for 60 s is invisible until it times out. This is a real
property of SLO alerting and is preserved deliberately.

A rule fires when **both** its long and short windows burn ≥ the rule's threshold; a detector fires when **any** rule fires.

| Detector | SLI | Rules (long, short, burn) |
|---|---|---|
| google_x60_{request,token,class} | per name | (60 s, 5 s, 14.4×) and (360 s, 30 s, 6×): the SRE Workbook (1 h, 5 m, 14.4×) and (6 h, 30 m, 6×) pairs compressed 60× so they fit a 10-minute run |
| tuned_{request,token,class} | per name | (120 s, 20 s, 6×) and (300 s, 60 s, 2.5×): longer short-windows and lower thresholds |
| fast_only_request | request | (30 s, 10 s, 8×) |

The Workbook's 3-day/6-hour slow-burn rule cannot be represented in a 10-minute run (limitation).

**Scoring.** A page is *true* if it starts inside `[incident.start, incident.end + 60 s]`, otherwise *false*.
`ttd` = first true page − incident start. False pages per hour are normalised by the non-incident time in the run.
Windows with fewer than `min_events` events are ignored, and each rule stays silent until `min(long window, 60 s)` of the run
has been observed (a replay starts with no history; a production alerter always has some). This start-up arming is a
documented threat to validity.

### Pre-registered predictions and what would falsify them
These are written **before running**; the author does not know the outcomes.

| | Prediction | Falsified if |
|---|---|---|
| H1 | In `s2_long_shift` the token-weighted and class detectors have a lower median TTD than the request detector at equal thresholds, or the request detector misses | the request detector's TTD is equal or lower and bootstrap intervals overlap |
| H2 | The `google_x60_*` detectors miss `s7_slow_burn` (burn ≈ 3× < 6×) while `tuned_*` catch it; `google_x60_*` show a higher false-page rate than `tuned_*` on the quiet periods of every run (the lab has no CRUD-traffic control, so "worse than CRUD" is **not** tested, only "worse than re-tuned") | `google_x60_*` catch s7, or `tuned_*` page falsely at least as often |
| H3 | The CPU policy consumes more error budget than queue-depth scaling in `s1_burst` and `s8_ramp`; burn-rate scaling reacts later than queue depth because the SLI lags demand | CPU consumes less than or equal budget, or intervals overlap |
| H4 | Stage attribution beats chance (1/3) and "always say llm" on retriever incidents (s5, s7); it abstains on full outages | accuracy on retriever incidents ≤ the always-llm baseline |

**Outcomes of these predictions (19 Nov 2026 run): [RESULTS.md](RESULTS.md).** The table above is left as written before any data existed.

### Low-traffic alerting: stretching windows (and what it costs)

The SRE Workbook's multi-window rules assume enough events per window. At laptop rates (0.1-0.3 req/s) a 5 s window holds
about one event, so a `min_events` guard of 5 means the FAST rule can never fire. The replay therefore stretches every
window of a detector by the same factor (`lab/analysis/detect.py: auto_time_scale`, factors 1, 2, 3, 4, 6), chosen as the
smallest factor for which the detector's shortest window is expected to hold `min_events` events, measured from the run's
own request count. Stretching keeps each rule's long:short ratio and burn threshold, only lowers time resolution, and
rules whose stretched long window no longer fits in 80 % of the run are DROPPED and reported (`rules_dropped`), never
truncated. This is a documented approximation of production behaviour, not a free lunch: it makes detectors slower by up to
the factor, so compare detectors within one report, and treat absolute TTD values as machine- and rate-specific.

## 5. Autoscaling policies (H3)

| Policy | Mechanism | Signal |
|---|---|---|
| static1 / static3 | fixed replicas | none (floor and ceiling references) |
| cpu | KEDA cpu trigger, Utilization 60% of the CPU **request** | CPU of the llm pods |
| queue | KEDA Prometheus trigger, AverageValue | `sum(slo_inflight)` (admitted-not-finished requests) per replica, threshold = parallel slots |
| burn | KEDA Prometheus trigger, Value | `max(slo:burn_rate:request_30s)`; HPA scales proportionally to burn/threshold |

Every KEDA run starts from exactly 1 replica and fresh pods (`set_policy` goes through static1 and restarts all workloads).
Cost is reported as mean ready replicas; effectiveness as error budget consumed over the run. A run whose replica count at
t=0 is unexpected is marked invalid.

## 6. Root-cause attribution baseline (H4)

Each successful request carries per-stage timings (retrieve, LLM time-to-first-token, decode, gateway overhead). For an incident,
the stage whose **median latency rose the most** versus the pre-incident baseline names the component. Fully failed
requests carry no stage data, so a total outage yields an abstention rather than a guess. It is compared with random (1/3)
and "always llm". It is a baseline to beat, not a claim of state-of-the-art RCA, and deliberately not distributed tracing.

## 7. Reproducibility

Run directory = `requests.csv` (ground truth), `events.json` (incidents + when faults were actually applied), `meta.json`
(seed, SLO and load config in force, tool versions, effective Helm values, git SHA, platform lockfile, validity reasons),
`replicas.csv`, optional `prom.json`. Runs are resumable (`DONE` marker). Run order is shuffled with a fixed seed to decorrelate runs
from laptop thermal drift. `platform/versions.lock` records the platform chart versions; `make pin-images` pins llama.cpp by digest.

## 8. Threats to validity (state these yourself before a reviewer does)

1. **Single shared node.** Load generator, Prometheus, Grafana and the model compete for CPU; thermal throttling and Windows background activity add noise. Compare policies within one report, not absolute numbers across machines.
2. **Small samples.** 3 repetitions give wide bootstrap intervals. Use ≥ 5 for headline claims and report the intervals.
3. **Time compression.** Burn-rate windows are scaled ×60; the statistical behaviour of a 60 s window with ~1 request/s differs from a 1 h window with thousands of requests (small-count noise is much larger). The `min_events` guard mitigates, not removes, this.
4. **Synthetic prompts and a 0.5B model.** Output quality is irrelevant here, but a small CPU model has a different cost profile than a GPU-served large model. Conclusions concern *method behaviour under heavy-tailed cost*, not production capacity planning.
5. **Fixed output length** (`ignore_eos`) removes output-length variance, which understates real heavy-tailedness.
6. **Offline replay of detectors** ignores alert-delivery latency and human response, and the start-up arming rule (§4) trades false pages for a short blind period.
7. **Capacity depends on the prompt mix**; `base_rps` is calibrated for the default mix, which is exactly what `s2` perturbs.
8. **Fault realism.** CPU stress and pod kill are real; netem may be unavailable on WSL2 (detected, then skipped, never emulated). Retriever faults are application-level injections.
9. **Statistical power is small by construction.** On a laptop CPU one replica serves ~0.2 req/s, so a 10-minute scenario holds 50-90 requests and a 99 % SLO has an error budget below one request. Detector comparisons therefore rest on windows stretched by different factors (the `x` and `rules` columns in every report) and on few events; use >= 5 repetitions for any claim and read results as exploratory evidence about method behaviour, not as benchmarks.
10. **Window stretching** (see §4) trades time resolution for statistical power at low request rates; TTD comparisons are valid between detectors in one report, not against production numbers.
11. **Capacity is a measured property of one machine on one day** (thermal state, power mode, background load). `make capacity` records its probe steps in `results/capacity/` and warns about host load; re-run it if the machine changed.
12. **Gateway is a single replica** (one coherent set of SLI counters) and is itself a possible bottleneck; the `max_lag` and gateway CPU limits are the checks.
13. **Fault intensity is derived from the calibrated threshold, so it can outgrow the plumbing.** s5 injects a retriever delay of 1.5 x the short-class TTFT threshold. A loose calibrated threshold (10.4 s) gives 15.6 s, which exceeded the gateway's old 10 s retriever timeout and turned the latency fault into an outage (every request failed with 503, no stage timings, nothing for the attribution baseline to read). The chart now sets `gateway.retrieveTimeoutS: 30` and the harness refuses to start a retriever-latency scenario when the delay would reach 90 % of the timeout actually configured on the running gateway (`k8s.retrieve_timeout_s`).
14. **The stage-attribution baseline (H4) is deliberately naive and is evaluated offline**: it answers whenever any stage median rose (no significance test), compares *pooled* medians (a few extra long prompts by chance can move a 30-request median by a few seconds. Phase A also showed a retriever-only fault scoring llm = +7 s; the code cannot explain that: the fault is an `asyncio.sleep` in the retriever and `llm_ttft_s` is measured from the gateway's own POST to llama.cpp, so the cause is UNVERIFIED. A prompt-mix shift is one candidate, a noisy neighbour on the shared laptop CPU another; the per-class stage medians of the recorded rows decide it) and only reads latencies of successful requests, so error-only faults (s7: 3 % retriever errors; s6: readiness eviction) are invisible to it. Because it is replayed from `requests.csv`, better estimators run on every recorded run without re-running anything. v2.5 adds two (`lab/analysis/rca.py`): `localize_adjusted` (each request against the baseline of its own prompt class) and `localize_tagged` (failures that name a hop first, class-adjusted latency second; `http_503` = retriever, `llm_error`/`timeout`/`llm_http_*` = llm). **They were designed after the Phase A data had been seen.** The report prints all three side by side; none of the improved numbers is a confirmatory result. A confirmatory test needs repetitions that were not used to design the estimators (fresh reps = fresh seeds, because the seed is `crc32(scenario:rep)`), evaluated once.
15. **Every scheduled request must leave a row.** The load generator records any failure of a single request (including unexpected exceptions) as a `client_error` row, and a run whose result has fewer rows than the schedule had arrivals is marked invalid (`n_scheduled` in `meta.json`). A failed request that silently vanished would understate the error rate exactly when the system is broken.

16. **Autoscaler triggers fire on plain baseline load.** `base_rps` is 0.75 x the single-replica capacity, so one replica is already 50-80 % utilised before any fault. Observed (s0_baseline, queue policy, threshold 2): scale-out at +24 s, mean 2.0 ready replicas, 1 replica for only 16 % of the run, 3 replicas once. `make simulate` predicts the same for cpu (60 % of a 1000 m request while llama.cpp burns ~2 cores when busy) and shows that the burn trigger is information-starved at ~5 requests per 30 s window: one bad request already reads as burn >= 20. A policy comparison between triggers that all scale on noise compares noise. `config/autoscaling.yaml` (see `docs/AUTOSCALING.md`) makes the thresholds editable; tune them on `s0_baseline` runs only, with a criterion fixed beforehand, and keep them for the whole of Phase B. Every run records the values it used (`meta.json` -> `tools.helm_values`).
17. **s6 is an outage, not a latency fault, on this stack.** Egress delay of 600 ms makes the llm readiness probe (1 s timeout; handshake + response = two delayed packets) fail, the pod leaves the Service endpoints (`replicas.csv`: ready 0 from +211 s to +321 s) and the gateway answers with connection errors (`llm_error`; 27 of the 28 requests sent in the window failed). It is reported as such; it is a useful *readiness-eviction* scenario, a poor *network-latency* scenario. Raising the probe timeout would keep the pod in rotation, but +1.2 s of RTT would then barely move a 10 s TTFT threshold: the fault would stop being visible to the SLI.
18. **A model, not a measurement.** `lab/sim.py` (processor-sharing pods, documented HPA algorithm, exponential service) reproduces the *direction* of the observed queue behaviour but is calmer than the real run (median first scale-out 58-126 s vs. +24 s observed), so its predictions about *quietness* are optimistic. It is used to rank candidate thresholds before spending hours of machine time, never to report a result.
19. **Detector false-page counts depend on the grace period** that separates a late page about the incident from an unrelated one (default 60 s). `make analyze` now prints the sensitivity (60/120/180 s) and `summarize --pages` lists each page with the bad requests that fired it, so a reader can see which false pages are echoes of the incident and which are not.
20. **Scale-in used to kill in-flight requests.** The llm pod had `terminationGracePeriodSeconds: 10` and no `preStop`, so a replica removed by the autoscaler received SIGTERM at once while it was still streaming. A tuning baseline under `burn` showed 1 failed request in 110 (0.9 %), about the whole error budget of a 99 % SLO, with replicas flapping between 3 and 1. That is a lab artefact, not a policy property (production LLM serving drains). v2.5.1 adds `llm.drainSeconds` (30 s `sleep` preStop) and `llm.terminationGraceSeconds` (45 s); `make verify` checks the live Deployment has it. The link between the failed request and a scale-in is consistent with the data but was not yet proven from the rows (`t_done` of the failure vs the replica timeline); Phase A is unaffected (static1 never scales in).
21. **A healthy baseline is a precondition for tuning or comparing anything.** The thresholds are 1.5 x the clean-baseline p99, so a one-replica `s0_baseline` at `base_rps` must be ~99 % good. Three tuning runs on 9 Nov showed 84-90 % good (p95 16-18 s) under all three policies, while the 8 Nov `queue` baseline had 98 % good with 2 replicas. Either the machine was slower that day (host load, power mode, thermal) or something in the lab is wrong; tuning triggers on a degraded baseline would tune them to noise. `docs/AUTOSCALING.md` section 4 now starts with a health gate (an `s0_baseline` + `static1` control run, >= 97 % good), and `make diag` reports leftover chaos experiments.

## 9. Where the code lives

| Concern | File |
|---|---|
| SLI/burn math | `lab/analysis/slo.py` |
| detectors + scoring | `lab/analysis/detect.py` |
| RCA baseline | `lab/analysis/rca.py` |
| run directories → tables | `lab/analysis/pipeline.py` |
| bootstrap CIs, report, figures | `lab/analysis/report.py` |
| open-loop generator | `lab/loadgen.py` |
| run lifecycle, fault scheduler, validity | `lab/runner.py` |
| capacity probe, SLO calibration | `lab/capacity.py` |
