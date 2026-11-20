# Confirmation runs: fixed BEFORE the new data exists

Written 18 Nov 2026, after the first complete Phase A (55 runs) and Phase B (60 runs, 3 reps per cell) had been analysed.
Nothing below may be changed after the confirmation runs have started. If it has to change, say so in the write-up and call the result exploratory.

## Why these runs
* **H4 (root-cause attribution).** The original estimator (`attribution_acc`, pooled stage medians) got 5/6 on `s5_retriever_latency` but 1/13 on
  `s7_slow_burn`, which is below the 1/3 chance level. Two other estimators were designed AFTER seeing those runs (`class_adjusted_acc`,
  `failure_tagged_acc`); `failure_tagged_acc` got 12/13 on s7. A result on the runs that were used to design an estimator is not a confirmation.
* **H3 (autoscaling).** The first 3 reps show the opposite of the hypothesis in `s3_cpu_stress` and `s8_ramp` (the CPU policy burned the least budget
  and ran about 2.6 replicas). Three reps give wide intervals; two more reps per cell in the two scenarios where policies differ.

## Fresh traffic
The seed is `crc32("scenario:rep")`, so every rep number that has not been run gives traffic nobody has seen:
`s5_retriever_latency` r7-r12, `s7_slow_burn` r14-r25, `s3_cpu_stress` and `s8_ramp` r4-r5 for all five policies.

```
make gate && { .venv/bin/slo-lab matrix --phase A --reps 12 --only s5_retriever_latency; \
               .venv/bin/slo-lab matrix --phase A --reps 25 --only s7_slow_burn; make gate; make health PHASE=A; }
make gate && { .venv/bin/slo-lab matrix --phase B --reps 5 --only s3_cpu_stress s8_ramp; make gate; make health PHASE=B; }
```

## Decision rules (primary outcome only)
1. **H4 confirmed** if, on the fresh `s5` + `s7` incidents together (6 + 12 = 18), `failure_tagged_acc` has a Wilson 95% lower bound above 1/3
   AND is higher than `always_llm_acc` (0 on retriever incidents). Otherwise: not confirmed, whatever the old runs said. The original `attribution_acc` is
   reported next to it either way.
2. **H3 reversal confirmed** if, over all (scenario, rep) pairs of `s3_cpu_stress` and `s8_ramp` (5 reps each, same traffic for every policy), the CPU policy
   burned less budget than `queue` AND than `burn` in at least 80% of the non-tied pairs, with an exact two-sided sign test p < 0.05 for each comparison.
   The replica price (`mean_replica_diff`) is always reported next to it: a policy that wins by running 2.6 replicas all the time has not "won" on cost.
3. Flagged runs: re-run once; a run that is flagged again is kept (`make accept-flagged`) and every table is shown with and without it (`make robust`).

## Not changed
Thresholds (queue 6, burn 10, cpu 60), SLO target 0.99, TTFT thresholds, detector definitions, the 60 s grace period, the x60 time compression.

## Addendum, 19 Nov 2026 (written before any Phase B confirmation run was started; Phase A confirmation had finished)
* Rule 2 counts reps 1-5, so 3 of its 5 reps are the ones that suggested the hypothesis. It is a pooled rule, not a clean replication. `make robust`
  therefore also prints the fresh reps alone.
* Fresh reps 4-5 are 4 pairs (2 scenarios x 2 reps). An exact sign test on 4 pairs cannot go below p = 0.125, so the fresh reps alone can only replicate the
  direction. With reps 4-6 there are 6 fresh pairs (smallest possible p = 0.031), which can reach p < 0.05 if all 6 go the same way.
* Decision rule 2 itself is unchanged. If the Phase B confirmation is run with `--reps 6` instead of `--reps 5` (30 new runs, about 5.4 h, instead of 20, about 3.6 h),
  rep 6 counts only in the fresh-reps line.

## Outcome, recorded 20 Nov 2026 (after all confirmation runs and the final analysis; the rules above are unchanged)
* **Rule 1 (H4): met.** `failure_tagged_acc` was 18/18 on the fresh `s5` (6) and `s7` (12) incidents, Wilson lower bound 0.82, against chance 0.33 and always-llm 0.00.
  Without the two accepted runs among them: 16/16 (0.81). The original `attribution_acc` scored 7/18 on the same incidents (`s7`: 1/12).
* **Rule 2 (H3 reversal): met for both comparisons.** Reps 1-5 of `s3_cpu_stress` and `s8_ramp`: cpu < queue in 10 of 10 pairs (p = 0.002, replica price +1.43);
  cpu < burn in 8 of 9 non-tied pairs (1 tie; p = 0.039; +0.98). The same with the accepted runs removed.
* **Fresh reps 4-6 alone (6 pairs):** cpu < queue 6 of 6 (p = 0.031). cpu < burn 4 of 5 non-tied (p = 0.38); five non-tied pairs cannot go below p = 0.0625, so this
  line could only show the direction.
* **Rule 3 (flagged runs):** 10 runs were flagged again after a re-run and were kept (Phase A 6, Phase B 4); one invalid run (`s3_cpu_stress__cpu__r6`) was re-run and is valid.
* Everything else is in [RESULTS.md](RESULTS.md).
