#!/usr/bin/env bash
# Read-only snapshot of everything that matters when a measurement looks wrong. Changes nothing.
# Output is also saved to results/diag-<time>.txt so it can be pasted or attached as it is.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker; need kubectl
mkdir -p "$ROOT/results"
OUT="$ROOT/results/diag-$(date -u +%Y%m%dT%H%M%SZ).txt"
sect() { printf '\n## %s\n' "$*"; }
{
  sect "time / load (idle should be well under 1.0)"; date -u; uptime
  sect "host"; nproc; free -m | head -2
  sect "running containers (only ${NODE} belongs to this lab)"; docker ps --format 'table {{.Names}}\t{{.Status}}' || true
  others="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -vx "$NODE" | tr '\n' ' ' || true)"
  if [ -n "${others// /}" ]; then
    echo "WARNING: these other containers are running and share your CPU: $others"
    echo "  stop them (nothing is deleted):  docker stop $others"
    echo "  and stop them coming back after a Docker restart:  docker update --restart=no $others"
  fi
  sect "container CPU/memory right now"; docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' || true
  sect "busiest processes inside WSL"; ps -eo pcpu,pmem,comm --sort=-pcpu | head -8 || true
  sect "model visible inside the node (must be yes; if NO run: make heal)"; if node_has_model; then echo yes; else echo NO; fi
  sect "lab pods"; k -n "$NS_APP" get pods -o wide || true
  sect "helm release history (a 'failed' line explains a run that died while preparing)"; h history slo-loop -n "$NS_APP" --max 6 2>&1 || true
  sect "recent events in $NS_APP"; k -n "$NS_APP" get events --sort-by=.lastTimestamp 2>&1 | tail -n 20 || true
  for d in llm gateway retriever; do
    sect "logs deploy/$d (last 12 lines)"; k -n "$NS_APP" logs "deploy/$d" --tail=12 2>&1 || true
  done
  sect "cluster CPU use"; k top pods -A --sort-by=cpu 2>&1 | head -12 || true
  sect "llm container CPU limit (cpu.max = quota period) and throttling (nr_throttled/nr_periods >> 0 means the limit is slowing llama.cpp)"
  k -n "$NS_APP" exec deploy/llm -c llama -- sh -c 'cat /sys/fs/cgroup/cpu.max; cat /sys/fs/cgroup/cpu.stat' 2>&1 || echo "(cgroup files not readable)"
  sect "leftover chaos experiments (must be EMPTY between runs: a stuck StressChaos/NetworkChaos silently degrades every later baseline)"
  k get stresschaos,networkchaos,podchaos -A 2>&1 || true
  sect "llm restarts"; k -n "$NS_APP" get pods -o custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount 2>&1 || true
  sect "config/load.yaml"; cat "$ROOT/config/load.yaml"
  sect "config/slo.yaml"; cat "$ROOT/config/slo.yaml"
  sect "finished runs"; find "$ROOT/results/runs" -name DONE 2>/dev/null | sed "s|$ROOT/||; s|/DONE||" | sort || true
} 2>&1 | tee "$OUT"
printf '\nSaved to %s\n' "$OUT"
