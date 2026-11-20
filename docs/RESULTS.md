# Results of the 19 November 2026 run

Read this together with [DESIGN.md](DESIGN.md) section 8 (threats to validity). Everything here comes from **one laptop** (16 GB, Windows 11 + WSL2,
1-node kind cluster, llama.cpp with Qwen2.5-0.5B Q4_K_M on CPU). Use the numbers to compare *methods with each other*; do not read any absolute latency
as a benchmark.

Source of every number: `results/ROBUSTNESS-digest.txt` (written by `make robust`; `make publish-results` copies it, the full report and the CSVs to
[`docs/results/`](results/)). Data collection was closed on 19 Nov 2026 (`make status` = DONE) and the tables below are from the final analysis.

## What was run

| | runs | what |
|---|---|---|
| Phase A (detection, attribution) | 73 | one replica (`static1`); s0 baseline x6, s1-s4 x6 each, s5 x12, s6 x6, s7 x25. 67 of them contain an incident. |
| Phase B (autoscaling) | 90 | 5 policies (`static1`, `static3`, `cpu`, `queue`, `burn`) x s1 (3 reps), s2 (3), s3 (6), s8 (6) |
| Health gates | 29 | one `s0_baseline` + `static1` run before every long batch (the batch commands are chained as `make gate && ...`) |

Traffic is a function of `crc32("scenario:rep")` only, so every policy and every detector sees *identical* arrivals for the same scenario and rep.
Detectors are replayed offline on the recorded requests, so detector comparisons are **paired**. Reps beyond the original plan (s5 r7-r12, s7 r14-r25,
s3 and s8 r4-r6) are the *fresh* reps used for the pre-registered confirmation ([PREREGISTRATION-confirmation.md](PREREGISTRATION-confirmation.md)).

## Verdicts at a glance

| | Pre-registered prediction (DESIGN section 4) | Verdict | Evidence in one line |
|---|---|---|---|
| **H1** | In `s2_long_shift` token / class detectors beat the request detector on time to detect, or the request detector misses | **Not demonstrated for s2** (6 runs, only 2 comparable). Overall the per-class SLI finds more incidents (borderline); the token SLI does not | tuned: class 58/67 vs request 51/67 (8 vs 1, p = 0.039); token 47/67 (p = 0.22). s2: 3/6, 2/6, 2/6 |
| **H2** | Workbook thresholds (x60) miss `s7_slow_burn` while tuned ones catch it; Workbook thresholds page falsely more often | **Half supported.** They do miss the slow burn. They are **not** noisier (second half falsified) | s7 request: 2/25 vs 18/25 (p < 0.001). False pages per quiet hour: request 1.12 vs 1.26, token 0.84 vs 1.12, class 1.82 vs 1.54 |
| **H3** | The CPU policy burns *more* budget than queue-depth scaling in s1 and s8; burn-rate scaling reacts later than queue | **Falsified, the opposite happened.** The reversal was confirmed under the pre-registered rule | cpu < queue in 10/10 pairs, cpu < burn in 8 of 9 non-tied (p = 0.002 / 0.039). Price: about 1.0-1.4 more replicas |
| **H4** | Stage attribution beats chance (1/3) and "always llm" on retriever incidents, and abstains on outages | **Latency-only baseline: not supported** (at chance on retriever incidents, below chance on `s7`). **Failure-tag attribution: confirmed** on fresh reps | naive 14/37 = 0.38 (CI 0.24-0.54); s5 12/12, s7 2/25. Tagged 18/18 fresh, Wilson lower bound 0.82 |

## How to read the tables

* **found** = incident runs in which a detector paged inside `[incident start, incident end + 60 s]`. **Wilson** 95% intervals are used for rates.
* **a_only / b_only** = runs only the first / only the second detector found. **p_sign** = exact two-sided sign test on those discordant runs
  (`0.000` in the digest means below 0.001). Paired, because both detectors saw the same requests.
* **ttd_diff_s** = median (first minus second) time to detect over the runs where *both* found the incident; negative = the first was sooner.
  Read it together with the number of runs behind it (often 2 to 25).
* **budget** = bad-request ratio / 1% in a run: 1.0 means the SLO was exactly met, 10 means ten times the allowed bad requests.
* **mean replicas** = mean number of ready llm replicas during the run (the cost side).
* **accepted run** = a run that was flagged (BAD / SLOW) on the first try *and again* after a re-run on a healthy machine, so it is a property of that
  traffic and not host noise. 10 runs: Phase A `s2_long_shift` r2, `s5_retriever_latency` r3, `s6_netem_delay` r4, `s7_slow_burn` r2 / r14 / r16;
  Phase B `s2_long_shift` r2 under `burn`, `cpu`, `queue`, `static1`. Every table below is also computed **without** them; each conclusion
  holds either way unless stated.

## Phase A: detection (H1, H2)

### All detectors, 67 incident runs

| detector | found | rate (95% CI) | `s7` slow burn | all but `s7` | false pages | per quiet hour |
|---|---|---|---|---|---|---|
| google_x60_request | 27/67 | 0.40 (0.29-0.52) | 2/25 | 25/42 | 8 | 1.12 |
| google_x60_token | 29/67 | 0.43 (0.32-0.55) | 2/25 | 27/42 | 6 | 0.84 |
| google_x60_class | 42/67 | 0.63 (0.51-0.73) | 10/25 | 32/42 | 13 | 1.82 |
| tuned_request | 51/67 | 0.76 (0.65-0.85) | 18/25 | 33/42 | 9 | 1.26 |
| tuned_token | 47/67 | 0.70 (0.58-0.80) | 14/25 | 33/42 | 8 | 1.12 |
| tuned_class | 58/67 | 0.87 (0.76-0.93) | 23/25 | 35/42 | 11 | 1.54 |
| fast_only_request (30 s / 10 s, 8x) | 55/67 | 0.82 (0.71-0.89) | 20/25 | 35/42 | 10 | 1.40 |

`google_x60_*` = the SRE Workbook rules (1 h / 5 m at 14.4x and 6 h / 30 m at 6x) compressed 60x so they fit a 10-minute run. `tuned_*` use longer short
windows and lower thresholds. Definitions: DESIGN section 4.

The ranking is the same in all three variants: all runs, without the accepted runs (61 incident runs), and only runs with host-logger data (48 runs).
Without the accepted runs the counts are tuned_class 53/61, fast_only 49/61, tuned_request 45/61, tuned_token 41/61, google_class 37/61,
google_token 25/61, google_request 23/61.

### H2: Workbook thresholds against thresholds tuned for this system (same SLI)

| pair | scope | found (tuned vs Workbook) | only tuned / only Workbook | p_sign | ttd_diff_s |
|---|---|---|---|---|---|
| request | all 67 | 51 vs 27 | 24 / 0 | < 0.001 | -9 |
| request | `s7` | 18 vs 2 | 16 / 0 | < 0.001 | +112 (2 runs, meaningless) |
| request | all but `s7` | 33 vs 25 | 8 / 0 | 0.008 | -10 |
| class | all 67 | 58 vs 42 | 17 / 1 | < 0.001 | -14 |
| class | `s7` | 23 vs 10 | 13 / 0 | < 0.001 | -128.5 |
| class | all but `s7` | 35 vs 32 | 4 / 1 | 0.38 | -10 |
| token | all 67 | 47 vs 29 | 18 / 0 | < 0.001 | -9 |
| token | `s7` | 14 vs 2 | 12 / 0 | < 0.001 | -7 |
| token | all but `s7` | 33 vs 27 | 6 / 0 | 0.031 | -9 |

**First half of the prediction holds.** The Workbook rules at x60 are almost deaf to the 3x slow burn in `s7`: 3% errors against a 1% budget is a burn rate of
about 3, below both rules' thresholds (14.4x and 6x). The tuned rules (6x and 2.5x on longer short windows) catch 14 to 23 of 25.

**Second half is falsified.** The pre-registration says the prediction fails if the tuned detectors "page falsely at least as often". They did, for request
(1.26 vs 1.12) and token (1.12 vs 0.84); only class went the predicted way (1.54 vs 1.82). These are 6 to 13 pages in about 7.1 quiet hours in total, so
none of the differences can be told apart from noise. The honest wording is: *the Workbook thresholds are not noisier here, they are blind to slow burns;
the tuned ones catch about twice as many incidents for a similar false-page rate.* Every detector paged falsely about once per quiet lab hour, so this system
is noisy for all of them.

**Artifacts removed.** Pages that fired within 30 s of the start of `s7` cannot be a slow burn (it needs minutes to build). They all come from the accepted
run `s7` r2 (Workbook request 8 s, class 8 s, token 21 s; tuned_token 8 s) and from `s7` r23 (tuned_class, 27 s, not an accepted run). Workbook request and
token therefore have at most 1 genuine `s7` find in 24 runs outside r2, and the tuned_class `s7` count (23/25) is at most 23 and possibly 22.

**The multi-window design bought nothing measurable here.** A single 30 s / 10 s rule (`fast_only_request`) found 55/67 (`s7`: 20/25) with 1.40 false pages
per quiet hour, i.e. more than `tuned_request` at a similar noise level. This is a statement about 10-minute runs at 0.2 requests per second, not about
production traffic.

### H1: token or class SLI against the request SLI (same thresholds)

| pair | scope | found (first vs second) | only first / only second | p_sign | ttd_diff_s |
|---|---|---|---|---|---|
| tuned class vs request | all 67 | 58 vs 51 | 8 / 1 | 0.039 | -15 |
| tuned class vs request | `s7` | 23 vs 18 | 5 / 0 | 0.062 | -125 |
| tuned class vs request | all but `s7` | 35 vs 33 | 3 / 1 | 0.62 | -6.5 |
| tuned class vs request | `s2_long_shift` | 3 vs 2 | 1 / 0 | 1.00 | -16.5 (2 runs) |
| tuned token vs request | all 67 | 47 vs 51 | 1 / 5 | 0.22 | 0 |
| tuned token vs request | `s7` | 14 vs 18 | 1 / 5 | 0.22 | -24 |
| tuned token vs request | `s2_long_shift` | 2 vs 2 | 0 / 0 | 1.00 | -12 (2 runs) |
| Workbook class vs request | all 67 | 42 vs 27 | 15 / 0 | < 0.001 | -6 |
| Workbook class vs request | `s7` | 10 vs 2 | 8 / 0 | 0.008 | -0.5 |
| Workbook token vs request | all 67 | 29 vs 27 | 2 / 0 | 0.50 | 0 |

* **The named test (`s2_long_shift`) is inconclusive.** The request detector found 2 of 6 runs, class 3, token 2. Only two runs were found by both, so the
  12 to 16 s timing advantage rests on n = 2.
* **Per-class SLI: a modest gain, not a confirmation.** At tuned thresholds it finds 8 more incidents than the request SLI and loses 1 (p = 0.039);
  inside `s7` the difference is not significant (p = 0.062). With the Workbook thresholds the gain is large (42 vs 27). The class detector also paged falsely
  most often (11 vs 9 for tuned request), so part of its gain may be sensitivity, not information. The data cannot separate the two.
* **Token SLI: no gain.** It finds fewer incidents than the request SLI (47 vs 51) and is no faster.

## Phase B: autoscaling (H3)

### Budget and cost by scenario (mean over reps; `without accepted` differs only where noted)

| scenario (reps) | metric | static1 | static3 | cpu | queue | burn |
|---|---|---|---|---|---|---|
| `s1_burst` (3) | budget | 27.89 | 11.77 | **10.65** | 21.97 | 26.52 |
| | mean replicas | 1.00 | 3.00 | 2.69 | 1.55 | 2.05 |
| `s2_long_shift` (3) | budget | 0.00 | 0.67 | 0.00 | 0.00 | 1.35 (2.02 without r2) |
| | mean replicas | 1.00 | 3.00 | 2.67 | 1.00 | 1.28 |
| `s3_cpu_stress` (6) | budget | 8.98 | 1.92 | **0.16** | 8.73 | 8.09 |
| | mean replicas | 1.00 | 3.00 | 2.53 | 1.11 | 1.73 |
| `s8_ramp` (6) | budget | 20.74 | 5.96 | **4.09** | 10.65 | 16.74 |
| | mean replicas | 1.00 | 3.00 | 2.74 | 1.33 | 1.62 |

No policy kept the SLO in `s1_burst` or `s8_ramp` (budget well above 1). In `s3_cpu_stress` only `cpu` did (0.16).

### Pre-registered confirmation rule (docs/PREREGISTRATION-confirmation.md, rule 2)

| scope | pair | non-tied pairs | cpu less / more / ties | p_sign | replica_diff | rule met |
|---|---|---|---|---|---|---|
| `s3` + `s8`, reps 1-5 (includes the 3 reps that suggested it) | cpu vs queue | 10 | 10 / 0 / 0 | 0.002 | +1.43 | **yes** |
| | cpu vs burn | 9 | 8 / 1 / 1 | 0.039 | +0.98 | **yes** |
| same, without accepted runs | cpu vs queue | 10 | 10 / 0 / 0 | 0.002 | +1.43 | yes |
| | cpu vs burn | 9 | 8 / 1 / 1 | 0.039 | +0.98 | yes |
| fresh reps 4-6 only (6 pairs) | cpu vs queue | 6 | 6 / 0 / 0 | 0.031 | +1.50 | n/a (replicates) |
| | cpu vs burn | 5 | 4 / 1 / 1 | 0.38 | +1.01 | no |

The registered rule is met for both comparisons, so the reversal is **confirmed as pre-registered**. Two caveats belong next to it:

* The pooled rule contains the three reps that suggested the hypothesis. On fresh reps alone, `cpu < queue` replicates (6 of 6, p = 0.031). `cpu < burn` held in 4
  of 5 non-tied fresh pairs (in one `s8` pair burn did better) but is not significant, and **could not have been**: five non-tied pairs cannot go below
  p = 0.0625. So it is "not confirmable on fresh reps alone", not "contradicted".
* The winner paid in replicas: the `cpu` policy ran about 1.0 to 1.4 replicas more than `burn` and `queue`.

### All 18 same-traffic pairs (s1, s2, s3, s8)

| pair | first burns less / more / ties | p_sign | mean budget diff | mean replica diff |
|---|---|---|---|---|
| cpu vs queue | 15 / 0 / 3 | < 0.001 | -6.93 | +1.41 |
| cpu vs burn | 14 / 1 / 3 | 0.001 | -9.73 | +0.98 |
| cpu vs static1 | 14 / 0 / 4 | < 0.001 | -11.36 | +1.65 |
| cpu vs static3 | 10 / 2 / 6 | 0.039 | -1.51 | -0.35 |
| queue vs static1 | 9 / 6 / 3 | 0.61 | -4.43 | +0.24 |
| burn vs static1 | 8 / 8 / 2 | 1.00 | -1.63 | +0.67 |
| burn vs queue | 4 / 11 / 3 | 0.12 | +2.80 | +0.43 |

* `cpu` beats every other policy, **including `static3`**, and does it with 0.35 fewer replicas on average.
* `queue` and `burn` are **statistically indistinguishable from never scaling** (`static1`). Their mean replica counts stay low (queue 1.0 to 1.6, burn 1.3 to 2.1).
* "Burn reacts later than queue" points the predicted way (burn burned more in 11 of 15 non-tied pairs) but is not significant (p = 0.12).
  An earlier interim table showed a stronger number; this final one replaces it.

**Likely reason (not tested here).** The CPU request is 1000m and llama.cpp uses about 2 cores while generating, so CPU utilisation reads "saturated" even
at baseline load (DESIGN section 8.16, `docs/AUTOSCALING.md`). The `cpu` policy therefore keeps about 2.5 to 2.7 replicas and sits at one replica only about
10% of the time: it behaves like "provision early and generously", which is why it ties `static3`. `burn` only sees requests that have *already finished*, so it
is slower than `queue` by construction. The thresholds (queue 6, burn 10, cpu 60%) were fixed before the experiment on quiet baselines, which is the fair
protocol, but it also makes `queue` and `burn` calm and therefore slow. The honest headline is **"scaling early and generously beat reacting to symptoms"**.
It is not "CPU is a good signal for LLM serving". A direct test would change the CPU target or request and pre-register the comparison (see future work).

## Phase A: root-cause attribution (H4)

| scenario (truth) | incidents | naive correct | always "llm" | failure-tagged |
|---|---|---|---|---|
| s1, s2, s3 (llm) | 6 each | 6/6 each | 6/6 | 6/6 each |
| s4_pod_kill (llm) | 6 | 4/6 | 6/6 | 5/6 |
| s5_retriever_latency (retriever) | 12 | **12/12** (CI 0.76-1.00) | 0 | 12/12 |
| s6_netem_delay (llm; an outage) | 6 | 0/6 (abstains) | 6/6 | 6/6 |
| s7_slow_burn (retriever) | 25 | **2/25** = 0.08 (CI 0.02-0.25) | 0 | 24/25 |
| retriever incidents, pooled | 37 | 14/37 = 0.38 (CI 0.24-0.54) | 0 | 36/37 |

Summed over all 67 incidents: naive 36, always-llm 30, failure-tagged 65 (these three sums are arithmetic on the columns above).
Without the accepted runs: naive 13/33 = 0.39 (0.25-0.56) on retriever incidents, failure-tagged 32/33; `s5` 11/11, `s7` 2/22.

* **Naive (stage latency medians): supported only where latency moves.** It is right on `s5` (12/12) and on the llm faults, but there it only matches "always llm".
  On `s7` it is below chance (0.33) because `s7` is an error-only fault: the retriever fails 3% of requests and the baseline reads latencies of *successful*
  requests only (DESIGN section 8.14 predicted this). Pooled over retriever incidents it is indistinguishable from chance. On `s6` it abstains on every run,
  which is the designed behaviour for an outage, but it scores as a miss.
* **The "always llm" baseline is 0 on retriever-only incidents by construction.** Beating it there proves nothing; chance (1/3) is the real bar.
* **Failure-tag attribution** (`localize_tagged`: failures that name a hop first, class-adjusted latency second; `http_503` = retriever,
  `llm_error` / `timeout` = llm) was designed **after** the first Phase A data. It was therefore tested once on fresh reps, with the rule fixed beforehand:
  18/18 on the fresh `s5` (6) and `s7` (12) incidents, Wilson lower bound 0.82, above chance (0.33) and above always-llm (0) -> **confirmed**. Without
  the two accepted runs among them: 16/16 (0.81), which the digest marks "so far" because 2 of the 18 are missing.
  The naive estimator scored 7/18 on the same fresh incidents (`s7`: 1/12).
* **What the confirmation does and does not say.** The tagged estimator reads which hop the failing request *names*. That is error classification, not
  inference from latency, and a 503 from the retriever is easy to attribute. It is the right tool for error faults and says nothing about silent slowdowns.

## What is confirmatory and what is not

* **Pre-registered before any data** (DESIGN section 4): the four predictions above. Outcomes: H1 not demonstrated, H2 half falsified, H3 falsified, H4 mixed.
* **Fixed before the confirmation runs** (18 Nov, addendum 19 Nov): the H4 failure-tag rule and the H3 reversal rule. Both met. The rules and the addendum were not
  changed afterwards.
* **Exploratory:** every other number, in particular all p-values in the H1 and H2 tables. There are roughly 40 sign tests in the digest and **no multiplicity
  correction**; treat p = 0.039 / 0.031 / 0.062 as suggestive. H1 and H2 had no fresh-rep confirmation.
* The estimators `class_adjusted` and `failure_tagged` and the reversal hypothesis for H3 were formed after looking at data; that is why they were
  re-tested on fresh traffic.

## What went wrong, and how it was handled

* **Unexplained host interference.** Health gates r20 and r21 failed (91.6% and 93.0% good, short-prompt median 0.98 s and 0.79 s against 0.55-0.83 s when
  healthy) while the WSL host columns looked idle. Later gates passed and a 5-hour batch ran cleanly. The cause was not found (Windows-side suspects: Defender,
  a database service, a browser, Docker Desktop). Mitigation: no batch starts without a passing gate, flagged runs are re-run on a healthy machine, runs
  that stay flagged are kept and reported with and without them.
* **10 accepted runs** (list above). A run that is flagged again after a re-run on a healthy machine has the same traffic and the same flag, so it is a
  property of the traffic draw. They are included in the main numbers and excluded in the "without accepted" variant.
* **One invalid run** (`s3_cpu_stress__cpu__r6`) was re-run; it is valid now.
* **A bug in the tooling** (accepted flags matched by run name only, so a Phase A name could silence a Phase B run) was found while reading the status
  output, fixed (keyed by phase and run), and covered by a regression test that fails when the fix is removed.
* **An interim analysis** at 12:34 IST on 19 Nov, before 6 flagged runs were re-run, was looked at. Which runs to re-run was decided by the written flag rules
  (host-health flags), not by results.
* **Claims withdrawn on the way:** several interim statements (e.g. "burn is worse than queue", "Google token found 0 of 24 on `s7`") changed with the final data
  and are replaced by the numbers in this document.

## Threats to validity beyond DESIGN section 8

1. **Small counts everywhere.** 6 runs per scenario for most Phase A scenarios, 3 for s1 / s2 in Phase B. A false-page comparison rests on 6-13 pages.
2. **Sign tests treat pairs as independent.** Pairs from the same scenario share its difficulty; the 18-pair p-values are optimistic.
3. **Mean budget is dominated by the big scenarios** (`s1`, `s8`, budgets of 10-28), so `mean_budget_diff` mostly reflects them; the pair *counts* are the more robust reading.
4. **Autoscaler thresholds were not tuned per policy** (queue 6, burn 10, cpu 60% were set once on quiet baselines). Other thresholds could change the ranking,
   especially for `queue` and `burn`.
5. **The cost side is replicas, not money.** No per-replica-second cost model was applied; `static3` was the only fixed over-provisioning reference.
6. **s7 and s6 are error faults.** Detection and attribution results on them depend on the 3% error injection and on the readiness-probe eviction, not on
   latency behaviour.
7. **Detector `found` counts include a few early pages** (see "Artifacts removed"); they do not change a conclusion but inflate individual cells.

## What this suggests (hypotheses for the next study, not findings)

* Burn-rate thresholds derived for high-volume services need re-deriving, or a single fast window, at low request rates; compressing the Workbook windows by a constant
  factor does not preserve their behaviour.
* A per-class SLI is worth its extra complexity only if its extra sensitivity is shown not to be extra noise.
* For a CPU-bound LLM server, an over-provisioning CPU policy is hard to beat on budget; judge autoscalers on budget **and** replica-seconds.
* Attribution needs an error taxonomy at the gateway, not just latency stages.

Future work, each to be pre-registered before it is run: CPU target 150% or CPU request 2000m to test the over-provisioning explanation; a per-replica-second
cost metric; per-policy threshold tuning under a fixed protocol; the same study on a GPU.

## Reproduce

```bash
make analyze && make robust       # results/REPORT.md, results/ROBUSTNESS.md, results/ROBUSTNESS-digest.txt, results/robust_*.csv
make publish-results              # copy them to docs/results/ so they can be committed
```
Raw runs are in `results/runs/` (git-ignored). `make share` packs everything into one zip.
