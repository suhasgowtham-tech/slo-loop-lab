SHELL := /bin/bash
VENV ?= .venv
PY   := $(VENV)/bin/python
LAB  := $(VENV)/bin/slo-lab
REPS ?= 3

.DEFAULT_GOAL := help
.PHONY: help tools venv preflight cluster model images platform deploy up verify require-verified chaos-preflight pin-images heal recover diag set-base autoscaling simulate share gate health rerun-bad hostlog status \
        latency profile capacity calibrate smoke phaseA phaseB analyze test lint down clean accept-flagged robust publish-results dashboards dashboards-build demo

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

tools: ## install kind, kubectl, helm 3 into ~/.local/bin (checksum-verified)
	scripts/01-install-tools.sh

venv: ## create .venv and install the harness (+ test deps)
	python3 -m venv $(VENV) || { echo "Run: sudo apt-get install -y python3-venv python3-pip"; exit 1; }
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e '.[dev]'

preflight: ## check WSL/Docker/RAM/CPU/disk/ports before installing anything
	scripts/00-preflight.sh

cluster: ## create the single-node kind cluster (pinned kube-context)
	scripts/02-create-cluster.sh

model: ## download + verify the 0.4 GB GGUF model
	scripts/03-download-model.sh

images: ## build gateway/retriever images and load them into kind
	scripts/05-build-images.sh

platform: ## install metrics-server, kube-prometheus-stack, KEDA, Chaos Mesh
	scripts/04-install-platform.sh

deploy: ## deploy the slo-loop-app Helm chart
	scripts/06-deploy-app.sh

up: tools venv preflight cluster model images platform deploy verify ## everything, in order, ending with the acceptance check

verify: ## acceptance check: platform, llama.cpp assumptions, e2e request, Prometheus, Grafana, CRD dry-runs
	scripts/verify.sh

heal: ## repair the lab after a Windows/WSL/Docker restart (empty model mount, crash-looping llm)
	scripts/heal.sh

recover: ## repair after a failed/interrupted run: heal + re-apply a 'failed' Helm release (then: make verify)
	$(LAB) recover

gate: require-verified ## HEALTH GATE (~12 min): one idle baseline run; fails (exit 1) below 97 % good. Chain long runs behind it:  make gate && make phaseB REPS=3
	$(LAB) gate

PHASE ?= B
health: ## per-run host-health table: which runs were measured on a slow host?  make health PHASE=B  (add `make hostlog` in a 2nd tmux window for CPU/steal/memory)
	$(LAB) health --phase $(PHASE)

rerun-bad: require-verified ## re-run the runs `make health` flags (BAD/SLOW/INVALID); refused unless a gate passed in the last 90 min:  make gate && make rerun-bad PHASE=B
	$(LAB) health --phase $(PHASE) --rerun

accept-flagged: ## keep runs that are STILL flagged BAD/SLOW after a re-run (reproducible, not host noise); refused unless a gate passed in the last 90 min:  make gate && make accept-flagged PHASE=B
	$(LAB) health --phase $(PHASE) --accept

hostlog: ## log host health (CPU busy/steal, free memory, busiest container) every 30 s to results/hostlog.csv; run it in a 2nd tmux window during long runs (Ctrl+C stops it)
	bash scripts/hostlog.sh

status: ## WHERE AM I? one screen: Docker, cluster, host logger, finished runs, last gate, and the ONE next command (read-only; run it after every restart)
	$(LAB) status

diag: ## read-only snapshot (load, containers, CPU throttling, config) saved to results/diag-*.txt: paste it when something looks off
	scripts/diag.sh

chaos-preflight: ## find out which faults your machine can really inject (netem?)
	scripts/chaos-preflight.sh

pin-images: ## pin the llama.cpp image by digest (reproducibility)
	scripts/pin-images.sh

require-verified: ## (internal) refuse to run experiments until `make verify` has passed on the current deployment
	@test -f .lab-verified || { echo "The lab has not been verified since the last deploy. Run: make verify"; exit 1; }

latency: require-verified ## measure idle per-class latency on this machine (~1-2 min); run this first
	$(LAB) latency

profile: ## show or switch the workload profile: make profile P=lite|standard
	$(LAB) profile $(P)

set-base: ## change base_rps = F x measured capacity without re-probing: make set-base F=0.75 (then make calibrate)
	$(LAB) set-base --frac $(or $(F),0.75)

capacity: require-verified ## measure single-replica capacity -> sets base_rps  (~10-20 min)
	$(LAB) capacity

calibrate: require-verified ## derive SLO TTFT thresholds from a clean baseline (10-20 min, length derived from base_rps)
	$(LAB) calibrate

smoke: require-verified ## 90 s end-to-end run + analysis of one run (proves the pipeline)
	$(LAB) run --phase X --scenario s0_baseline --policy static1 --rep 1 --duration 90 --force
	$(LAB) summarize results/runs/X/s0_baseline__static1__r1

dashboards: ## where are the 3 Grafana dashboards, are they loaded, does Prometheus have what they draw? (read-only, ~2 s)  make dashboards ARGS=--open opens the first one
	$(LAB) dashboards $(ARGS)

dashboards-build: ## regenerate the 3 dashboard JSON files from lab/dashgen.py (dashboards as code); tests fail if the committed JSON differs
	$(PY) -m lab.dashgen

demo: require-verified ## guided 4.5-minute incident for the dashboards and a screen recording: one retriever fault, alert to recovery (phase X, not part of the results)
	$(LAB) demo $(ARGS)

autoscaling: require-verified ## apply config/autoscaling.yaml (cpu/queue/burn thresholds) to the running lab and print the effective values
	$(LAB) autoscaling

simulate: ## predict (no cluster needed) how cpu/queue/burn triggers behave at baseline load and in a burst
	$(LAB) simulate

share: ## pack results/ + config/ into ONE zip (copied to Windows Downloads) so you can attach it to a chat instead of screenshots
	$(LAB) share

phaseA: require-verified ## detection experiments (H1/H2/H4); resumable
	$(LAB) matrix --phase A --reps $(REPS)

phaseB: require-verified ## autoscaling experiments (H3); resumable
	$(LAB) matrix --phase B --reps $(REPS)

analyze: ## build results/REPORT.md + figures from finished runs
	$(LAB) analyze

robust: ## paired statistics + every table with/without the accepted runs -> results/ROBUSTNESS.md (run after make analyze)
	$(LAB) robust

publish-results: ## copy the finished tables, CSVs and figures to docs/results/ (the part you commit to git; run make analyze && make robust first)
	@test -f results/REPORT.md -a -f results/ROBUSTNESS.md || { echo "Run 'make analyze && make robust' first: results/REPORT.md or results/ROBUSTNESS.md is missing."; exit 1; }
	@mkdir -p docs/results
	@cp results/REPORT.md results/ROBUSTNESS.md docs/results/
	@for f in results/ROBUSTNESS-digest.txt results/accepted-flags.csv results/robust_*.csv results/phase*.csv results/*.png; do [ -f "$$f" ] && cp "$$f" docs/results/; done; true
	@echo "[publish-results] $$(ls docs/results | wc -l) files in docs/results/. Raw runs stay out of git. Next: git add docs/results docs/RESULTS.md README.md && git commit"

test: ## unit + local end-to-end tests (no cluster needed)
	$(PY) -m pytest

lint: ## static checks: yamllint, shellcheck, helm lint + helm template
	$(VENV)/bin/yamllint -c .yamllint config scenarios chaos platform/values charts/slo-loop-app/values.yaml
	@if command -v shellcheck >/dev/null; then shellcheck -x scripts/*.sh; else echo "shellcheck not installed (sudo apt-get install -y shellcheck) - skipped"; fi
	helm lint charts/slo-loop-app -f config/slo.yaml
	@for p in static1 static2 static3 cpu queue burn; do helm template slo-loop charts/slo-loop-app -n slo-loop -f config/slo.yaml --set autoscaling.policy=$$p >/dev/null && echo "  rendered policy $$p"; done

down: ## delete the kind cluster (keeps model + results)
	scripts/teardown.sh

clean: ## remove caches and the virtualenv (keeps results)
	rm -rf $(VENV) .pytest_cache **/__pycache__ *.egg-info
