# SLO-Loop Lab

**Do classic SRE reliability methods survive LLM-serving workloads, and where exactly do they break?**

A reproducible, ₹0, single-laptop lab (Windows 11 + WSL2 + Docker Desktop, 16 GB RAM) that serves a small
quantised LLM on Kubernetes, injects real faults, and measures how well error budgets, multi-window
multi-burn-rate alerts and autoscaling policies behave on that traffic.

```
 open-loop load  ──►  gateway (FastAPI, SLIs) ──►  retriever (fault-injectable)
 generator            │                           │
 (Poisson, 3 prompt   └──────────►  llama.cpp server (Qwen2.5-0.5B Q4_K_M, CPU, 1-3 replicas)
  classes)                                  ▲
                                            │  Chaos Mesh: CPU stress · pod kill · netem delay
 Prometheus + Grafana + KEDA (cpu | queue-depth | burn-rate scaling) on a 1-node kind cluster
```

Everything is open source and free: kind, Helm, kube-prometheus-stack, KEDA, Chaos Mesh, llama.cpp,
and a 0.4 GB Apache-2.0 model. Nothing is exposed beyond `127.0.0.1`.

## Read this first: what is and is not verified

| Verified in CI-style checks (done before you received this) | Only your laptop can verify (`make verify` does it for you) |
|---|---|
| 300+ pytest tests: SLI math, burn-rate windows, detectors (incl. low-traffic window stretching), scoring, RCA baseline, load generator, the adaptive capacity probe, workload profiles, deployment guards, and a full local end-to-end run (real gateway + retriever processes, a **fake** llama.cpp, fault injection, detection, RCA) | That the **real** llama.cpp image accepts the flags and streams the format the gateway expects (`make verify` probes it) |
| Helm chart rendered through Go's real `text/template` engine for all 6 policies; no duplicate resources, selectors/ports line up, NodePorts match the kind mapping, every PromQL expression parses, every rule/metric name the dashboard and KEDA use is defined | `helm lint` / `helm install` with the **real Helm binary** (it could not be downloaded in the build sandbox) |
| yamllint and shellcheck clean; built-in Kubernetes kinds validated against the K8s 1.33 schemas | kind/Docker Desktop behaviour, image builds, KEDA/Chaos Mesh behaviour, WSL2 kernel features such as netem |
| The tests were **mutation-checked**: 10 deliberately re-introduced chart bugs were all caught | Whether your machine reproduces the results below. Absolute latencies, capacity and thresholds are specific to one laptop; the *relative* findings are what to compare. |

The results below are from one run on one laptop (19 Nov 2026). Any figure you publish must come from your own runs; yours will differ in absolute numbers.

## Requirements

- Windows 11 with WSL2 (Ubuntu 24.04 recommended) and Docker Desktop (WSL2 backend; free for personal use)
- 16 GB RAM (WSL gets 12 GB), **≥ 6 logical CPUs recommended** (≥ 4 works with caveats), ≥ 25 GB free disk
- Keep the repo **inside the Linux filesystem** (`~/slo-loop-lab`), never under `/mnt/c`

## Quickstart (about 30-45 minutes the first time; downloads roughly 6 GB)

**1. Windows (PowerShell)**

```powershell
wsl --install -d Ubuntu-24.04          # reboot if asked, create your Linux user
notepad $env:UserProfile\.wslconfig    # paste docs\wslconfig.example, set processors to your CPU, save
wsl --shutdown
```

Docker Desktop → Settings → General: *Use the WSL 2 based engine*. Settings → Resources → WSL integration:
enable *Ubuntu-24.04*. Start Docker Desktop and wait until it says *running*.

**2. WSL (Ubuntu)**

```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip curl jq make git unzip
cd ~ && unzip /mnt/c/Users/<you>/Downloads/slo-loop-lab.zip && cd slo-loop-lab
make up
```

`make up` runs, in order: `tools` (kind, kubectl, Helm 3 → `~/.local/bin`, checksum-verified) → `venv` →
`preflight` (stops with the exact fix if RAM, CPU, disk, ports, Docker or location are wrong) → `cluster` →
`model` (downloads and verifies the GGUF) → `images` → `platform` (metrics-server, kube-prometheus-stack, KEDA,
Chaos Mesh) → `deploy` → `verify`. **If `verify` is not fully green, stop and read
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md); do not run experiments on an unverified lab.**

Dashboards (localhost only): Grafana <http://127.0.0.1:3000> (admin / `slo-loop-lab`, dashboard *SLO-Loop Lab*),
Prometheus <http://127.0.0.1:9090>, gateway <http://127.0.0.1:8080>.

## Running the study

```bash
make verify            # must be green (it also unlocks the commands below; a redeploy locks them again)
make chaos-preflight   # which faults your kernel can really inject (netem often cannot on WSL2; s6 is then skipped, never faked)
make latency           # ~1-3 min: what ONE request of each class costs on YOUR machine (tells you if the workload is too heavy)
make capacity          # ~10-20 min: starts from the latency estimate, finds single-replica capacity, sets base_rps = 0.75 x capacity (`make set-base F=0.6` changes the fraction without re-probing)
make calibrate         # ~10-25 min: derives per-class TTFT thresholds from YOUR clean baseline
make smoke             # ~4 min: one 90 s run + analysis; proves the whole pipeline on your machine
make status           # WHERE AM I? read-only: Docker, cluster, host logger, finished runs per phase, last gate, and the ONE next command. Run it after every restart
make accept-flagged PHASE=B # keep runs that are STILL flagged after a re-run (reproducible, not host noise); needs a passing gate; INVALID runs are never accepted
make robust                # after make analyze: paired statistics (exact sign tests, Wilson intervals) and EVERY table with/without the accepted runs -> results/ROBUSTNESS.md
make publish-results       # after analyze + robust: copy REPORT.md, ROBUSTNESS.md, the CSVs and the figures to docs/results/ (the part you commit to git)
make hostlog          # in a 2nd tmux window (Ctrl+b then c) during long runs: logs CPU busy/steal and free memory every 30 s
make rerun-bad PHASE=B # re-run the runs that `make health` flags (BAD/SLOW/INVALID); needs a passing gate in the last 90 min; hands-off
make health PHASE=B   # per-run table: which runs were measured on a slow host (flags BAD / SLOW / INVALID)
make gate              # ~12 min HEALTH GATE: one idle baseline run, exit 1 below 97 % good. Run it before every long session, and chain:
                       #   (inside tmux: `tmux new -s lab`)   make gate && make phaseB REPS=3 ; make gate     (docs/TROUBLESHOOTING.md: tmux, power, Docker restarts)
make phaseA REPS=3     # detection experiments, ~5-6 h, resumable (Ctrl-C and re-run continues)
make phaseB REPS=3     # autoscaling experiments, ~14 h, resumable; start with REPS=2 or --only a scenario
make analyze           # results/REPORT.md + figures
```

Workload size: `make profile` lists the request-size profiles (`lite` is the default; `standard` is ~3x heavier and only
sensible on a fast CPU). Switching profile invalidates capacity and thresholds, so repeat `latency`, `capacity`, `calibrate`.
`make capacity` is adaptive: it starts from the measured idle latency, retreats downward if its first step is already
degraded, and stops with a concrete diagnosis rather than a traceback.

`make diag` is a read-only snapshot (load, running containers, per-container CPU, CPU throttling of the llm pod, your measured config)
saved to `results/diag-*.txt`: run it whenever a number looks odd, and paste it instead of describing the problem.
After a run, `.venv/bin/slo-lab summarize <run-dir>` also prints how the autoscaler moved the llm replicas over time.

**After any Windows / WSL / Docker Desktop restart, run `make heal` first** (it fixes the empty model mount the restart can leave
behind, and restarts the pods), then `make verify`. Experiment commands refuse to run until `make verify` is green.

Reading one run: `.venv/bin/slo-lab summarize results/runs/A/<run> --pages` prints the per-class latency, the request outcomes (overall and inside the fault window),
every detector's verdict, the three attribution estimators, the autoscaler's behaviour (mean replicas, time at one replica) and, with `--pages`, each page with a TRUE/FALSE verdict and the
bad requests that fired it. `make simulate` predicts (no cluster needed) whether the cpu/queue/burn triggers would scale on plain baseline load; `make autoscaling` applies your own
thresholds from `config/autoscaling.yaml` (template: `config/autoscaling.yaml.example`); both are explained in `docs/AUTOSCALING.md`.
`make share` packs `results/` and your measured `config/` into ONE zip (copied to your Windows Downloads folder when that is unambiguous): attach that file instead of photographing the terminal, so every table can be re-computed from the raw requests.

If a run dies (for example `Helm release 'slo-loop' is in state 'failed'`), `make recover` repairs the lab and `make verify` re-checks it;
then re-run the same `make phaseX` command: finished runs are skipped. Since v2.4 the matrix does one repair + one retry by itself,
skips a scenario that cannot work with the current deployment (with the reason), stops after two failed jobs in a row, and writes
`results/matrix-failures.log`.

Run long phases overnight with the laptop plugged in and sleep disabled; close browsers and other heavy apps
(the load generator reports its own scheduling lag and **invalidates** a run it could not drive faithfully).
Subset example: `.venv/bin/slo-lab matrix --phase B --reps 2 --only s1_burst s8_ramp`.

## Hypotheses (full operationalisation in [docs/DESIGN.md](docs/DESIGN.md))

| | Claim under test | Where |
|---|---|---|
| **H1** | Request-count SLIs hide the pain of long-prompt users; token-weighted (or per-class) budgets detect a prompt-mix shift sooner | Phase A, `s2_long_shift` |
| **H2** | Standard (SRE-Workbook) burn-rate thresholds mis-time or mis-fire on heavy-tailed LLM latency; re-tuned windows fix part of it | Phase A, all scenarios, 7 detectors replayed on identical traffic |
| **H3** | CPU-based autoscaling is a poor signal for LLM serving; queue-depth and burn-rate-aware scaling burn less error budget per replica-second | Phase B, 5 policies × 4 scenarios |
| **H4** | A trivial stage-attribution baseline localises the faulty component well above chance, and fails in identifiable cases | Phase A, incidents with ground-truth root cause |

The design pre-registers what would **falsify** each claim. The honest outcome may be "no difference" or
"the opposite"; both are publishable findings.

## Results (one run, one laptop, 19 Nov 2026)

73 detection runs and 90 autoscaling runs, paired on identical traffic. Full tables, caveats and the list of things that went wrong:
**[docs/RESULTS.md](docs/RESULTS.md)** (raw tables in [docs/results/](docs/results/) after `make publish-results`).

| | Verdict against the pre-registered prediction | Key numbers |
|---|---|---|
| **H1** token / class SLIs | **Not demonstrated** for the named scenario (`s2`: 6 runs, 2 comparable). The per-class SLI finds more incidents overall (borderline); the token SLI does not | tuned class 58/67 vs request 51/67 (p = 0.039); token 47/67 |
| **H2** Workbook thresholds (x60) | **Half supported.** They are blind to the slow burn; they are *not* noisier than tuned ones | `s7`: 2/25 vs 18/25 (p < 0.001); false pages per quiet hour 1.12 vs 1.26 (request) |
| **H3** CPU autoscaling is a poor signal | **Falsified, the opposite.** CPU scaling burned the least budget, at the price of about 1 to 1.4 more replicas; queue and burn scaling were no better than never scaling | cpu < queue 10/10, cpu < burn 8 of 9 non-tied (p = 0.002 / 0.039); 18 pairs: cpu < static3 10-2 with 0.35 fewer replicas |
| **H4** stage attribution | **Latency-only baseline not supported** (chance level on retriever incidents, below chance on the error-only `s7`). **Failure-tag attribution confirmed** on fresh reps | naive 14/37; tagged 18/18 fresh (Wilson lower bound 0.82) |

Read the caveats before quoting any of it: single shared node, small counts, no multiplicity correction on the H1 / H2 tests, and the "why" for H3
(CPU target vs a 1000m request makes the CPU policy an over-provisioner) is an untested explanation.

## Scenarios

| Id | Fault | Root cause label |
|---|---|---|
| s0_baseline | none (false-page measurement) | - |
| s1_burst | 3× traffic for 120 s | llm |
| s2_long_shift | long prompts 10% → 50% at constant request rate | llm |
| s3_cpu_stress | Chaos Mesh StressChaos in one llm pod | llm |
| s4_pod_kill | Chaos Mesh PodChaos | llm |
| s5_retriever_latency | retriever latency = 1.5 × short-class threshold | retriever |
| s6_netem_delay | Chaos Mesh NetworkChaos +600 ms (needs kernel netem). Trips the readiness probe: in practice an outage (see DESIGN §8.17) | llm |
| s7_slow_burn | retriever errors 3% for 360 s (≈ 3× burn) | retriever |
| s8_ramp | stepped demand ramp to 3× and back | llm |

## Repository layout

```
apps/gateway, apps/retriever   the two Python services (+ Dockerfiles)
charts/slo-loop-app            Helm chart: workloads, KEDA ScaledObjects, ServiceMonitors, SLO recording rules, Grafana dashboard
platform/                      kind config + pinned-by-lockfile values for the platform charts
chaos/                         Chaos Mesh manifests (blast radius: namespace slo-loop only)
config/                        slo.yaml · load.yaml · detectors.yaml · matrix.yaml (single source of truth)
scenarios/                     one YAML per scenario (load segments, faults, ground-truth incidents)
lab/                           harness: open-loop loadgen, runner, capacity/calibration, analysis, CLI
scripts/                       numbered install steps + verify.sh (acceptance check)
tests/                         pytest suite (+ Go chart renderer, fake llama.cpp)
results/                       runs/ (raw, git-ignored), REPORT.md, ROBUSTNESS.md and figures (yours; `make publish-results` copies the tables to docs/results/)
docs/                          DESIGN.md · RESULTS.md · PREREGISTRATION-confirmation.md · AUTOSCALING.md · TROUBLESHOOTING.md · results/ (published tables)
```

## Safety properties

- Every `kubectl`/`helm` call is pinned to the context `kind-slo-loop`, never your EKS/AKS/GKE contexts.
- Chaos Mesh only honours experiments in namespaces annotated for it; only `slo-loop` is.
- All published ports bind to `127.0.0.1`. `make down` deletes the cluster and keeps your model and results.
- Platform chart versions are recorded in `platform/versions.lock`; run metadata records tool versions, the
  git SHA and the effective Helm values next to every run. `make pin-images` pins llama.cpp by digest.

## Honest limitations

One node means the load generator, Prometheus and the model share CPU; results are valid for comparing policies
**within one report on one machine**, not as absolute latency claims. Burn-rate windows are time-compressed ×60.
The RCA baseline uses stage timings, not distributed traces. Repetitions: 6 per Phase A scenario (s5: 12, s7: 25) and 3 (s1, s2) or 6 (s3, s8)
per Phase B cell, so intervals are wide. See *Threats to validity* in [docs/DESIGN.md](docs/DESIGN.md).

## Using this honestly on a résumé

Write what you ran and measured ("built a Kubernetes lab and measured X over N runs"), link the repo and the report,
and be ready to explain every design choice in an interview; the DESIGN document is written to help with that.
Do not list results you have not produced. Report the predictions that failed (H3 was falsified, H2 half falsified) as plainly as the ones that held.

Licence: MIT (see `LICENSE`). Third-party components keep their own licences.
