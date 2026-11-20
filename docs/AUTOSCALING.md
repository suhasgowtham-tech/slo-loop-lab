# Autoscaler triggers at low traffic (H3): what was observed, what is predicted, how to tune them fairly

Phase B compares scaling policies on error-budget burn. That comparison is only meaningful if each trigger is quiet when nothing is wrong.
This page records why the shipped thresholds are not, and the protocol for choosing better ones **without tuning on the experiment itself**.

## 1. What was observed (reference machine, `make` run `s0_baseline`, policy `queue`, chart defaults)

No fault, 101 requests at `base_rps` = 0.178 (0.75 x the measured single-replica capacity of 0.237 req/s):

```
llm replicas (seconds since start: desired/ready):  -3s 1/1  +24s 2/1  +29s 2/2  +324s 1/1  +385s 2/1  +390s 2/2  +448s 3/2  +454s 3/3  +565s 2/2  +597s 1/1
```

Mean 2.02 ready replicas, exactly one replica for 16 % of the run, three replicas once. The autoscaler reacted to plain load, 24 s after the run started.
Policy `queue` with threshold 2 therefore means "scale whenever 3 requests happen to be in flight", and at 52 % utilisation that happens all the time.
The `cpu` and `burn` policies had not been measured at baseline when this was written; section 3 predicts them and section 4 measures them.

## 2. How each trigger turns a signal into a replica count

KEDA creates a HorizontalPodAutoscaler (HPA); the HPA syncs every 15 s, ignores changes within +-10 % of the target, scales up by at most 3 pods
per 15 s and scales down to the **maximum recommendation of the last `scaleDownStabilizationSeconds` (120 s)**, one pod per 30 s.

| policy | signal | desired replicas | why it is noisy here |
|---|---|---|---|
| `queue` | `sum(slo_inflight)` (admitted, not finished; includes requests waiting for one of `llm.parallel` = 2 slots) | `ceil(inflight / threshold)` (AverageValue) | at 52 % utilisation 3 or more requests are in flight a large share of the time; one sample is enough to scale up |
| `burn` | `max(slo:burn_rate:request_30s)` | `ceil(ready * burn / threshold)` (Value) | a 30 s window holds ~5 requests: **one** bad request is 20 % bad = burn 20, so the threshold hardly matters (2 and 10 behave the same) |
| `cpu` | mean pod CPU / CPU **request** (1000m) | `ceil(ready * utilisation / 60 %)` | llama.cpp burns ~2 cores while generating, so a replica that is busy half the time already shows ~90 % |

## 3. What `make simulate` predicts (a model, not a measurement)

`lab/sim.py` re-implements the HPA algorithm above on processor-sharing pods (assumptions are listed at the top of that file and in DESIGN.md, item 18).
Numbers below: 200 simulated 600 s runs per row, `base_rps` 0.178, solo service 2.93 s, ~2 % of requests bad at baseline (the measured 98 % good),
3x burst from 200 s to 320 s (scenario s1). "first scale-out" is measured from the start of the burst; 0 s means it had already scaled on baseline load.

| trigger | threshold | baseline: mean replicas | time at 1 replica | runs that ever scale | burst: mean replicas | burst: first scale-out |
|---|---|---|---|---|---|---|
| queue | 2 (chart default) | 1.57 | 54% | 96% | 1.92 | 1 s |
| queue | 4 | 1.15 | 85% | 62% | 1.59 | 31 s |
| queue | 6 | 1.05 | 95% | 24% | 1.46 | 31 s |
| queue | 8 | 1.02 | 98% | 8% | 1.38 | 46 s |
| burn, 30 s window | 2 (chart default) | 1.52 | 72% | 81% | 1.95 | 61 s |
| burn, 30 s window | 20 | 1.18 | 87% | 51% | 1.81 | 91 s |
| burn, 120 s window | 6 | 1.34 | 81% | 50% | 1.96 | 76 s |
| burn, 120 s window | 10 | 1.14 | 91% | 24% | 1.90 | 106 s |
| burn, 120 s window | 20 | 1.03 | 98% | 8% | 1.76 | 151 s |
| cpu | 60 % (chart default) | 2.80 | 6% | 100% | 2.83 | 0 s |
| cpu | 100 % | 2.24 | 11% | 100% | 2.47 | 0 s |
| cpu | 150 % | 1.68 | 34% | 100% | 2.01 | 0 s |

How far to trust it: for `queue` with threshold 2 the model says 54 % of the time at one replica; the real run had 16 %. **The model is calmer than the
machine**, so a value that looks marginal in the table will be worse in reality. Read the table for *ranking and direction*, then measure.

Two things the table already shows, independent of tuning:

* `burn` is slower than `queue` by construction. A burn rate is computed from requests that have already *finished* (late or failed); an in-flight count sees the
  queue grow before anything finishes. With a 120 s window the first scale-out arrives ~100 s into a 120 s burst. That is a finding for H3, not a tuning failure.
* `cpu` cannot be made quiet with a 1000m request: even a 150 % target scales on baseline load (see section 5).

## 4. Protocol: choose thresholds once, on s0_baseline only

### 4-0. Health gate (do this first)

The thresholds were set to 1.5 x the clean-baseline p99, so one replica at `base_rps` must be ~99 % good. If it is not, the machine (or something in the lab) differs
from the machine that was calibrated, and **every trigger will look noisy for a reason that has nothing to do with the trigger**.
First tuning round on the reference machine (9 Nov, candidate thresholds, `s0_baseline`, 110 requests each): queue 6 -> 81 % of the run at one replica (criterion 90 %, fail),
burn(120 s) 10 -> 17 % (fail), cpu 60 -> 2 % (expected), but **good% was only 84.5 / 87.3 / 90.0 %** (p95 15.8 / 18.2 / 17.1 s) against 98.0 % (p95 9.4 s) for the 8 Nov `queue` baseline.
At ~13 % bad requests the burn rate of a 120 s window is ~13 on average, so burn threshold 10 cannot be quiet; raising it to 20 would only move the problem.
The burn run also had 1 failed request (0.9 %), see DESIGN item 20 (scale-in used to cut streams; fixed in v2.5.1).

```bash
make diag                                                               # idle snapshot: load, containers, leftover chaos, throttling
.venv/bin/slo-lab run --phase X --scenario s0_baseline --policy static1 --rep 4 --force
.venv/bin/slo-lab summarize results/runs/X/s0_baseline__static1__r4 | grep -E "^(class|short|medium|long|all)"
make share                                                              # attach the zip: today's run is compared with Phase A's s0_baseline__static1 r1-r3
```

Gate: `good%` of the `all` row >= 97 % (with ~110 requests the chance of more than 3 bad ones at a true 1 % is ~2 %). Do not tune or run Phase B below it. Keep the laptop plugged in,
on the Best-performance power mode, and otherwise idle during runs (browser, video calls and Windows updates all share the CPU the lab measures).

### 4-1. Tune

Rule fixed before any Phase B run (write it into the report): **a trigger passes when the llm tier stays at exactly one replica for >= 90 % of an `s0_baseline` run.**
Never tune on s1/s2/s3/s8 (the scenarios the policies are compared on); responsiveness is judged from the simulation only.

```bash
cd ~/slo-loop-lab

# 4a. (only after the health gate) before: the two policies measured with the chart defaults (2 x ~12 min)
for p in cpu burn; do .venv/bin/slo-lab run --phase X --scenario s0_baseline --policy $p --rep 1 --force; done
for p in queue cpu burn; do echo "== $p"; .venv/bin/slo-lab summarize results/runs/X/s0_baseline__${p}__r1 | grep -E "^all|autoscaler"; done

# 4b. apply the candidate thresholds (a full helm upgrade, no pod restarts) and check them (3 x ~12 min)
cp config/autoscaling.candidate.yaml config/autoscaling.yaml
make autoscaling
for p in queue cpu burn; do .venv/bin/slo-lab run --phase X --scenario s0_baseline --policy $p --rep 2 --force; done
for p in queue cpu burn; do echo "== $p"; .venv/bin/slo-lab summarize results/runs/X/s0_baseline__${p}__r2 | grep -E "^all|autoscaler"; done
```

Read the `autoscaler:` line: `exactly 1 replica for NN % of the run`. If a trigger is below 90 %, raise its threshold one step (queue 6 -> 8; burn 10 -> 20) in
`config/autoscaling.yaml`, run `make autoscaling` and repeat 4b for that policy with `--rep 3`. Two consecutive passes freeze the file for all of Phase B;
every run records the values it used in `meta.json` (`tools.helm_values`), so a changed file is visible afterwards.

`make autoscaling` prints the effective values the deployed release uses (it is `helm get values --all`), so you can see that your file really took effect.

## 5. The cpu trigger

HPA utilisation is relative to the CPU **request** (1000m), while one llama.cpp replica uses up to its limit (2000m) whenever it is generating. The signal therefore lives
between 0 % (idle) and 200 % (saturated) and has no headroom: busy half the time = ~90 %. The simulation shows no target <= 150 % that is quiet at baseline.
Options, in order of how much they change the experiment:

1. **Keep 60 % and report it** (the candidate file does): "the Kubernetes-default CPU policy is mismatched to a CPU-bound server", which is a legitimate H3 result.
2. Raise the target as far as 150 % and report that even then it scaled on baseline load.
3. Raise `llm.resources.requests.cpu` to 2000m for the whole lab (then 60 % = 1200m, which a half-busy replica does not reach). This changes scheduling for **every** policy
   and needs 6 allocatable cores for three replicas (`kubectl describe node | grep -A8 Allocatable`); it is deliberately not done automatically.

Decide before Phase B and state the choice in the report; do not change it afterwards.

## 6. What to say in the write-up

* The chart-default thresholds scaled on plain baseline load (section 1, measured), so a comparison using them would compare noise.
* Thresholds were chosen with a stated criterion on `s0_baseline` only (section 4) and frozen.
* Responsiveness numbers in section 3 are a model; the measured numbers are the Phase B results.
* With ~100 requests per run and 3 repetitions the Phase B differences are exploratory; use >= 5 repetitions for any headline claim.
