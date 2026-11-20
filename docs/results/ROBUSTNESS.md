# SLO-Loop Lab robustness and paired statistics

Variants: `all` = every valid run; `minus_accepted` = without the runs kept after a re-run (`make accept-flagged`); `host_logged_only` = only runs that have host-logger samples (Phase A only below). A conclusion should hold in all of them.

Left out: minus_accepted drops 10 run(s); host_logged_only drops 20 Phase A run(s).


## Confirmation on fresh reps (pre-registered)

Rules: docs/PREREGISTRATION-confirmation.md.

- H4 [fresh pooled]: failure_tagged 18/18, Wilson lower bound 0.82 vs chance 0.33 and always-llm 0.00 -> CONFIRMED
- H4 [fresh pooled, without accepted]: failure_tagged 16/16, Wilson lower bound 0.81 vs chance 0.33 and always-llm 0.00 -> CONFIRMED so far (INCOMPLETE: 16 of 18 fresh incidents)
- H3 [registered rule: reps 1-5 (includes the 3 reps that suggested it)]: cpu < queue: 10/10 non-tied ok; cpu < burn: 8/9 non-tied ok -> CONFIRMED
- H3 [registered rule, without accepted]: cpu < queue: 10/10 non-tied ok; cpu < burn: 8/9 non-tied ok -> CONFIRMED
- H3 [fresh reps 4+ only]: cpu burned less in 6/6 non-tied pairs vs queue and 4/5 non-tied pairs vs burn (direction NOT replicated in every pair); not significant on fresh pairs alone


| group | incidents | naive | class_adj | failure_tagged | tag_ci_lo | tag_ci_hi | always_llm | chance |
|---|---|---|---|---|---|---|---|---|
| s5_retriever_latency | 6 | 6/6 | 6/6 | 6/6 | 0.61 | 1.00 | 0/6 | 0.33 |
| s7_slow_burn | 12 | 1/12 | 2/12 | 12/12 | 0.76 | 1.00 | 0/12 | 0.33 |
| fresh pooled | 18 | 7/18 | 8/18 | 18/18 | 0.82 | 1.00 | 0/18 | 0.33 |
| fresh pooled, without accepted | 16 | 7/16 | 8/16 | 16/16 | 0.81 | 1.00 | 0/16 | 0.33 |


| scope | pair | pairs | cpu_less | cpu_more | ties | share_less | p_sign | min_p_possible | replica_diff | rule_met |
|---|---|---|---|---|---|---|---|---|---|---|
| registered rule: reps 1-5 (includes the 3 reps that suggested it) | cpu vs queue | 10/10 | 10 | 0 | 0 | 1.00 | 0.00 | 0.00 | 1.43 | True |
| registered rule: reps 1-5 (includes the 3 reps that suggested it) | cpu vs burn | 10/10 | 8 | 1 | 1 | 0.89 | 0.04 | 0.00 | 0.98 | True |
| registered rule, without accepted | cpu vs queue | 10/10 | 10 | 0 | 0 | 1.00 | 0.00 | 0.00 | 1.43 | True |
| registered rule, without accepted | cpu vs burn | 10/10 | 8 | 1 | 1 | 0.89 | 0.04 | 0.00 | 0.98 | True |
| fresh reps 4+ only | cpu vs queue | 6/6 | 6 | 0 | 0 | 1.00 | 0.03 | 0.03 | 1.50 | True |
| fresh reps 4+ only | cpu vs burn | 6/6 | 4 | 1 | 1 | 0.80 | 0.38 | 0.06 | 1.01 | False |


## Phase A - detectors on the same incident runs (variant: all)

| detector | found | rate | ci_lo | ci_hi | macro_rate | s7_slow_burn | all_but_s7 | false_pages | false_per_quiet_hr |
|---|---|---|---|---|---|---|---|---|---|
| fast_only_request | 55/67 | 0.82 | 0.71 | 0.89 | 0.80 | 20/25 | 35/42 | 10 | 1.40 |
| google_x60_class | 42/67 | 0.63 | 0.51 | 0.73 | 0.68 | 10/25 | 32/42 | 13 | 1.82 |
| google_x60_request | 27/67 | 0.40 | 0.29 | 0.52 | 0.46 | 2/25 | 25/42 | 8 | 1.12 |
| google_x60_token | 29/67 | 0.43 | 0.32 | 0.55 | 0.51 | 2/25 | 27/42 | 6 | 0.84 |
| tuned_class | 58/67 | 0.87 | 0.76 | 0.93 | 0.82 | 23/25 | 35/42 | 11 | 1.54 |
| tuned_request | 51/67 | 0.76 | 0.65 | 0.85 | 0.75 | 18/25 | 33/42 | 9 | 1.26 |
| tuned_token | 47/67 | 0.70 | 0.58 | 0.80 | 0.72 | 14/25 | 33/42 | 8 | 1.12 |


`rate` is pooled over incident runs (s7 has 13 reps, the others 6, so it weighs most); `macro_rate` gives every scenario the same weight; `false_per_quiet_hr` divides all false pages by all non-incident time of all runs.


## Phase A - H1 - token / class SLI against the request SLI (same thresholds)

| pair | scope | runs | a_found | b_found | a_only | b_only | p_sign | both | ttd_a_minus_b_median_s |
|---|---|---|---|---|---|---|---|---|---|
| tuned_class vs tuned_request | ALL | 67 | 58 | 51 | 8 | 1 | 0.04 | 50 | -15.00 |
| tuned_class vs tuned_request | s7_slow_burn | 25 | 23 | 18 | 5 | 0 | 0.06 | 18 | -125.00 |
| tuned_class vs tuned_request | all but s7 | 42 | 35 | 33 | 3 | 1 | 0.62 | 32 | -6.50 |
| tuned_token vs tuned_request | ALL | 67 | 47 | 51 | 1 | 5 | 0.22 | 46 | 0.00 |
| tuned_token vs tuned_request | s7_slow_burn | 25 | 14 | 18 | 1 | 5 | 0.22 | 13 | -24.00 |
| tuned_token vs tuned_request | all but s7 | 42 | 33 | 33 | 0 | 0 | 1.00 | 33 | 0.00 |
| google_x60_class vs google_x60_request | ALL | 67 | 42 | 27 | 15 | 0 | 0.00 | 27 | -6.00 |
| google_x60_class vs google_x60_request | s7_slow_burn | 25 | 10 | 2 | 8 | 0 | 0.01 | 2 | -0.50 |
| google_x60_class vs google_x60_request | all but s7 | 42 | 32 | 25 | 7 | 0 | 0.02 | 25 | -7.00 |
| google_x60_token vs google_x60_request | ALL | 67 | 29 | 27 | 2 | 0 | 0.50 | 27 | 0.00 |
| google_x60_token vs google_x60_request | s7_slow_burn | 25 | 2 | 2 | 0 | 0 | 1.00 | 2 | 6.50 |
| google_x60_token vs google_x60_request | all but s7 | 42 | 27 | 25 | 2 | 0 | 0.50 | 25 | 0.00 |


## Phase A - H2 - workbook thresholds (x60) against thresholds tuned for this system (same SLI)

| pair | scope | runs | a_found | b_found | a_only | b_only | p_sign | both | ttd_a_minus_b_median_s |
|---|---|---|---|---|---|---|---|---|---|
| tuned_request vs google_x60_request | ALL | 67 | 51 | 27 | 24 | 0 | 0.00 | 27 | -9.00 |
| tuned_request vs google_x60_request | s7_slow_burn | 25 | 18 | 2 | 16 | 0 | 0.00 | 2 | 112.00 |
| tuned_request vs google_x60_request | all but s7 | 42 | 33 | 25 | 8 | 0 | 0.01 | 25 | -10.00 |
| tuned_class vs google_x60_class | ALL | 67 | 58 | 42 | 17 | 1 | 0.00 | 41 | -14.00 |
| tuned_class vs google_x60_class | s7_slow_burn | 25 | 23 | 10 | 13 | 0 | 0.00 | 10 | -128.50 |
| tuned_class vs google_x60_class | all but s7 | 42 | 35 | 32 | 4 | 1 | 0.38 | 31 | -10.00 |
| tuned_token vs google_x60_token | ALL | 67 | 47 | 29 | 18 | 0 | 0.00 | 29 | -9.00 |
| tuned_token vs google_x60_token | s7_slow_burn | 25 | 14 | 2 | 12 | 0 | 0.00 | 2 | -7.00 |
| tuned_token vs google_x60_token | all but s7 | 42 | 33 | 27 | 6 | 0 | 0.03 | 27 | -9.00 |


`a_only` / `b_only` = incident runs found by only the first / only the second detector; `p_sign` is the exact two-sided sign test on those; `ttd_a_minus_b_median_s` < 0 means the first detector fired sooner (runs where both found it). Per-scenario rows are in the CSVs.


## Phase A - do the detection rates depend on which runs are used?

| detector | all | minus_accepted | host_logged_only |
|---|---|---|---|
| fast_only_request | 55/67 (82%) | 49/61 (80%) | 40/48 (83%) |
| google_x60_class | 42/67 (63%) | 37/61 (61%) | 33/48 (69%) |
| google_x60_request | 27/67 (40%) | 23/61 (38%) | 23/48 (48%) |
| google_x60_token | 29/67 (43%) | 25/61 (41%) | 24/48 (50%) |
| tuned_class | 58/67 (87%) | 53/61 (87%) | 41/48 (85%) |
| tuned_request | 51/67 (76%) | 45/61 (74%) | 39/48 (81%) |
| tuned_token | 47/67 (70%) | 41/61 (67%) | 36/48 (75%) |


## Phase A - s7 slow burn: pages within 30 s of the incident start (check these)

| scenario | run | detector | ttd_s | accepted |
|---|---|---|---|---|
| s7_slow_burn | s7_slow_burn__static1__r2 | google_x60_class | 8.00 | True |
| s7_slow_burn | s7_slow_burn__static1__r2 | google_x60_request | 8.00 | True |
| s7_slow_burn | s7_slow_burn__static1__r2 | google_x60_token | 21.00 | True |
| s7_slow_burn | s7_slow_burn__static1__r2 | tuned_token | 8.00 | True |
| s7_slow_burn | s7_slow_burn__static1__r23 | tuned_class | 27.00 | False |


A page that early usually comes from trouble before the incident, not from the incident. Check a run with `.venv/bin/slo-lab summarize results/runs/A/<run> --pages`; `accepted=True` marks a run that was kept after a re-run.


## Phase A - H4 attribution with intervals (variant: all)

| scenario | truth | incidents | correct | acc | ci_lo | ci_hi | answered | acc_if_answered | always_llm | chance | exploratory_tagged |
|---|---|---|---|---|---|---|---|---|---|---|---|
| s1_burst | llm | 6 | 6/6 | 1.00 | 0.61 | 1.00 | 6 | 1.00 | 1.00 | 0.33 | 6/6 |
| s2_long_shift | llm | 6 | 6/6 | 1.00 | 0.61 | 1.00 | 6 | 1.00 | 1.00 | 0.33 | 6/6 |
| s3_cpu_stress | llm | 6 | 6/6 | 1.00 | 0.61 | 1.00 | 6 | 1.00 | 1.00 | 0.33 | 6/6 |
| s4_pod_kill | llm | 6 | 4/6 | 0.67 | 0.30 | 0.90 | 5 | 0.80 | 1.00 | 0.33 | 5/6 |
| s5_retriever_latency | retriever | 12 | 12/12 | 1.00 | 0.76 | 1.00 | 12 | 1.00 | 0.00 | 0.33 | 12/12 |
| s6_netem_delay | llm | 6 | 0/6 | 0.00 | 0.00 | 0.39 | 0 | n/a | 1.00 | 0.33 | 6/6 |
| s7_slow_burn | retriever | 25 | 2/25 | 0.08 | 0.02 | 0.25 | 20 | 0.10 | 0.00 | 0.33 | 24/25 |
| RETRIEVER pooled | retriever | 37 | 14/37 | 0.38 | 0.24 | 0.54 | 32 | 0.44 | 0.00 | 0.33 | 36/37 |


Same, retriever incidents only, WITHOUT the accepted runs:

| scenario | truth | incidents | correct | acc | ci_lo | ci_hi | answered | acc_if_answered | always_llm | chance | exploratory_tagged |
|---|---|---|---|---|---|---|---|---|---|---|---|
| s5_retriever_latency | retriever | 11 | 11/11 | 1.00 | 0.74 | 1.00 | 11 | 1.00 | 0.00 | 0.33 | 11/11 |
| s7_slow_burn | retriever | 22 | 2/22 | 0.09 | 0.03 | 0.28 | 18 | 0.11 | 0.00 | 0.33 | 21/22 |
| RETRIEVER pooled | retriever | 33 | 13/33 | 0.39 | 0.25 | 0.56 | 29 | 0.45 | 0.00 | 0.33 | 32/33 |


`acc` counts an abstention as a miss (as in `make analyze`); `acc_if_answered` does not. `chance` = 1/3. `exploratory_tagged` is the estimator that was designed AFTER the Phase A data had been seen: do not quote it as a result, only as the reason to collect fresh reps.


## Phase B - budget consumed (x the budget; 1.0 = SLO exactly met), with and without the accepted runs

| scenario | policy | n | budget | replicas | n_minus_accepted | budget_minus_accepted |
|---|---|---|---|---|---|---|
| s1_burst | burn | 3 | 26.52 | 2.05 | 3 | 26.52 |
| s1_burst | cpu | 3 | 10.65 | 2.69 | 3 | 10.65 |
| s1_burst | queue | 3 | 21.97 | 1.55 | 3 | 21.97 |
| s1_burst | static1 | 3 | 27.89 | 1.00 | 3 | 27.89 |
| s1_burst | static3 | 3 | 11.77 | 3.00 | 3 | 11.77 |
| s2_long_shift | burn | 3 | 1.35 | 1.28 | 2 | 2.02 |
| s2_long_shift | cpu | 3 | 0.00 | 2.67 | 2 | 0.00 |
| s2_long_shift | queue | 3 | 0.00 | 1.00 | 2 | 0.00 |
| s2_long_shift | static1 | 3 | 0.00 | 1.00 | 2 | 0.00 |
| s2_long_shift | static3 | 3 | 0.67 | 3.00 | 3 | 0.67 |
| s3_cpu_stress | burn | 6 | 8.09 | 1.73 | 6 | 8.09 |
| s3_cpu_stress | cpu | 6 | 0.16 | 2.53 | 6 | 0.16 |
| s3_cpu_stress | queue | 6 | 8.73 | 1.11 | 6 | 8.73 |
| s3_cpu_stress | static1 | 6 | 8.98 | 1.00 | 6 | 8.98 |
| s3_cpu_stress | static3 | 6 | 1.92 | 3.00 | 6 | 1.92 |
| s8_ramp | burn | 6 | 16.74 | 1.62 | 6 | 16.74 |
| s8_ramp | cpu | 6 | 4.09 | 2.74 | 6 | 4.09 |
| s8_ramp | queue | 6 | 10.65 | 1.33 | 6 | 10.65 |
| s8_ramp | static1 | 6 | 20.74 | 1.00 | 6 | 20.74 |
| s8_ramp | static3 | 6 | 5.96 | 3.00 | 6 | 5.96 |


## Phase B - H3 policy against policy on the same traffic (all scenarios together)

| pair | scope | pairs | a_burns_less | a_burns_more | ties | p_sign | mean_budget_diff | mean_replica_diff |
|---|---|---|---|---|---|---|---|---|
| cpu vs queue | ALL | 18 | 15 | 0 | 3 | 0.00 | -6.93 | 1.41 |
| cpu vs burn | ALL | 18 | 14 | 1 | 3 | 0.00 | -9.73 | 0.98 |
| cpu vs static1 | ALL | 18 | 14 | 0 | 4 | 0.00 | -11.36 | 1.65 |
| cpu vs static3 | ALL | 18 | 10 | 2 | 6 | 0.04 | -1.51 | -0.35 |
| queue vs static1 | ALL | 18 | 9 | 6 | 3 | 0.61 | -4.43 | 0.24 |
| burn vs static1 | ALL | 18 | 8 | 8 | 2 | 1.00 | -1.63 | 0.67 |
| burn vs queue | ALL | 18 | 4 | 11 | 3 | 0.12 | 2.80 | 0.43 |


Same, WITHOUT the accepted runs:

| pair | scope | pairs | a_burns_less | a_burns_more | ties | p_sign | mean_budget_diff | mean_replica_diff |
|---|---|---|---|---|---|---|---|---|
| cpu vs queue | ALL | 17 | 15 | 0 | 2 | 0.00 | -7.34 | 1.40 |
| cpu vs burn | ALL | 17 | 14 | 1 | 2 | 0.00 | -10.30 | 0.94 |
| cpu vs static1 | ALL | 17 | 14 | 0 | 3 | 0.00 | -12.03 | 1.65 |
| cpu vs static3 | ALL | 17 | 10 | 2 | 5 | 0.04 | -1.60 | -0.35 |
| queue vs static1 | ALL | 17 | 9 | 6 | 2 | 0.61 | -4.69 | 0.25 |
| burn vs static1 | ALL | 17 | 8 | 8 | 1 | 1.00 | -1.73 | 0.71 |
| burn vs queue | ALL | 17 | 4 | 11 | 2 | 0.12 | 2.97 | 0.46 |


`a_burns_less` = (scenario, rep) pairs where the first policy used less budget; ties (e.g. 0 against 0) are left out of the test. `mean_replica_diff` is the price: the extra ready replicas the first policy ran on average. Per-scenario rows are in robust_paired_budget.csv.


## Read this before quoting any number
- Exact sign tests on a handful of pairs: a result is only as strong as its smallest p and its weakest variant. No multiple-comparison correction.
- Same traffic does not mean independent samples: reps of one scenario share structure. Use these as evidence for the next experiment, not as a proof.
- A detector rate that moves a lot between variants is a result about the runs, not about the detector.
