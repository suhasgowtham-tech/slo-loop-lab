#!/usr/bin/env bash
# shellcheck shell=bash disable=SC2034
# Shared helpers. Sourced by every script; never executed directly. (SC2034: variables are used by the sourcing scripts.)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${LAB_CLUSTER:-slo-loop}"
KCTX="${LAB_KUBE_CONTEXT:-kind-${CLUSTER}}"
NS_APP="slo-loop"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"
PROM_PORT="${PROM_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
NODE="${CLUSTER}-control-plane"
MODEL_FILE="${LAB_MODEL_FILE:-Qwen2.5-0.5B-Instruct-Q4_K_M.gguf}"
export PATH="$HOME/.local/bin:$PATH"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

# Every cluster call is pinned to the lab's kind context: other contexts (EKS/AKS/GKE) are never touched.
k() { kubectl --context "$KCTX" "$@"; }
h() { helm --kube-context "$KCTX" "$@"; }

# The llm pod reads the model from /models INSIDE the kind node, which is a bind mount of ./models. After a Windows /
# WSL / Docker Desktop restart Docker can re-create that mount before the Ubuntu distro is up and leave it EMPTY;
# the llm pod then crash-loops with "failed to open GGUF file ... No such file or directory". `make heal` repairs it.
node_has_model() { docker exec "$NODE" test -s "/models/$MODEL_FILE" >/dev/null 2>&1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing tool '$1' (run: make tools)"; }

# While Docker Desktop is stopped, still starting or has its WSL integration switched off, `docker` is only a stub that prints
# "could not be found in this WSL 2 distro". That is NOT a lost cluster: the kind node and your results are untouched.
docker_up() { docker info >/dev/null 2>&1; }
require_docker() {
  need docker
  docker_up || die "Docker is not reachable from this Ubuntu (Docker Desktop stopped, still starting, or WSL integration off). Start Docker Desktop, wait for 'Engine running', check Settings > Resources > WSL integration > Ubuntu, then run this again. Your cluster and results are safe; this is NOT a lost cluster."
}

require_cluster() {
  need kubectl
  if command -v docker >/dev/null 2>&1 && ! docker_up; then require_docker; fi
  k get ns kube-system >/dev/null 2>&1 || die "cluster context '$KCTX' is not reachable (run: make cluster)"
}

diagnose() {
  warn "diagnostics for namespace $NS_APP:"
  k -n "$NS_APP" get pods -o wide 2>&1 | sed 's/^/    /' || true
  k -n "$NS_APP" get events --sort-by=.lastTimestamp 2>&1 | tail -n 15 | sed 's/^/    /' || true
  for d in llm gateway retriever; do
    printf '    --- logs deploy/%s (last 15 lines)\n' "$d"
    k -n "$NS_APP" logs "deploy/$d" --tail=15 2>&1 | sed 's/^/    /' || true
  done
}
