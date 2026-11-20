#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need helm; need kubectl; need docker
require_cluster
bold "Deploying slo-loop-app"
rm -f "$ROOT/.lab-verified"   # state is changing: verification no longer holds

[ -f "$ROOT/models/${LAB_MODEL_FILE:-Qwen2.5-0.5B-Instruct-Q4_K_M.gguf}" ] || die "model not downloaded (run: make model)"
node_has_model || die "the model is not visible inside the kind node ($NODE:/models). This happens after a Windows/WSL/Docker restart. Fix: make heal"
k get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1 || die "Prometheus Operator CRDs missing (run: make platform)"

args=(upgrade --install slo-loop "$ROOT/charts/slo-loop-app" -n "$NS_APP" --create-namespace
  --reset-values -f "$ROOT/config/slo.yaml")
# optional overlays, in precedence order: slo.yaml < autoscaling.yaml (your trigger thresholds) < images.yaml (digest pins)
if [ -f "$ROOT/config/autoscaling.yaml" ]; then
  "$ROOT/.venv/bin/python" -c "from lab import k8s; k8s.autoscaling_overlay()" || die "config/autoscaling.yaml is invalid (see the message above)"
  args+=(-f "$ROOT/config/autoscaling.yaml")
  ok "using your trigger thresholds from config/autoscaling.yaml"
fi
[ -f "$ROOT/config/images.yaml" ] && args+=(-f "$ROOT/config/images.yaml")
args+=(--wait --timeout 8m)

if ! h "${args[@]}"; then diagnose; die "deploy failed (see diagnostics above and docs/TROUBLESHOOTING.md)"; fi

# Application images use a fixed tag + IfNotPresent: restart so a rebuilt image is always picked up.
k -n "$NS_APP" rollout restart deploy/gateway deploy/retriever >/dev/null
for d in gateway retriever llm; do k -n "$NS_APP" rollout status "deploy/$d" --timeout=300s >/dev/null || { diagnose; die "deploy/$d did not become ready"; }; done
ok "all workloads ready"
k -n "$NS_APP" get pods
