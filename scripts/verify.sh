#!/usr/bin/env bash
# End-to-end acceptance check of the whole lab. Exits non-zero if anything required is broken.
# Every check prints what it verified, so a green run is evidence, not a promise.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need kubectl; need curl; need jq; need helm; need docker
require_cluster
PASS=0; FAIL=0; WARNS=0
rm -f "$ROOT/.lab-verified"   # a failed or interrupted run must never leave a stale "verified" marker

check() { # description, command...
  local d="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$d"; PASS=$((PASS + 1)); else printf '  \033[31m✘ %s\033[0m\n' "$d"; FAIL=$((FAIL + 1)); fi
}
soft() { warn "$*"; WARNS=$((WARNS + 1)); }
retry() { # seconds, command...  (re-run until success or timeout)
  local secs="$1"; shift; local end=$((SECONDS + secs))
  while [ "$SECONDS" -lt "$end" ]; do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 3; done
  return 1
}

bold "1/6  Platform"
check "node Ready" k wait --for=condition=Ready node --all --timeout=30s
for pair in monitoring:monitoring keda:keda chaos-mesh:chaos-mesh; do
  ns="${pair%%:*}"
  check "all deployments Available in $ns" k -n "$ns" wait --for=condition=Available deploy --all --timeout=120s
done
check "metrics-server Available" k -n kube-system wait --for=condition=Available deploy/metrics-server --timeout=60s
check "chaos-daemon DaemonSet rolled out" k -n chaos-mesh rollout status ds/chaos-daemon --timeout=60s
check "Prometheus pod Ready" k -n monitoring wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus --timeout=120s
for crd in servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com scaledobjects.keda.sh \
  stresschaos.chaos-mesh.org podchaos.chaos-mesh.org networkchaos.chaos-mesh.org; do
  check "CRD $crd" k get crd "$crd"
done
check "metrics-server serves 'kubectl top' (needed by the CPU policy)" retry 90 k top nodes

bold "2/6  Application"
check "model file is visible inside the kind node (if not: make heal)" node_has_model
for d in llm gateway retriever; do check "deploy/$d Available" k -n "$NS_APP" wait --for=condition=Available "deploy/$d" --timeout=60s; done

llm_has_prestop() { [ "$(k -n "$NS_APP" get deploy llm -o jsonpath='{.spec.template.spec.containers[0].lifecycle.preStop.exec.command[0]}' 2>/dev/null)" = sleep ]; }
check "llm drains in-flight requests when scaled in (preStop is deployed; if not: make autoscaling)" llm_has_prestop
check "llm image provides the drain command (sleep)" k -n "$NS_APP" exec deploy/llm -c llama -- sleep 0

bold "3/6  llama.cpp assumptions (probed inside the cluster)"
probe="$(k -n "$NS_APP" exec -i deploy/gateway -c gateway -- python - < "$ROOT/scripts/probe_llm.py" 2>&1 || true)"
printf '%s\n' "$probe" | sed 's/^/    /'
if printf '%s\n' "$probe" | grep -q '^HEALTH 200'; then ok "llama.cpp /health = 200"; PASS=$((PASS + 1)); else printf '  \033[31m✘ /health did not return 200\033[0m\n'; FAIL=$((FAIL + 1)); fi
if printf '%s\n' "$probe" | grep -q '^COMPLETION 200'; then ok "POST /completion (stream) = 200"; PASS=$((PASS + 1)); else printf '  \033[31m✘ /completion did not return 200\033[0m\n'; FAIL=$((FAIL + 1)); fi
if printf '%s\n' "$probe" | grep -E '^FINAL ' | grep -Eq '"(stop|stopped_limit)": true'; then ok "stream ends with a stop=true chunk (the gateway relies on this)"; PASS=$((PASS + 1)); else printf '  \033[31m✘ no stop=true final chunk: the gateway would mark every request llm_incomplete. Paste the RAW lines above into an issue.\033[0m\n'; FAIL=$((FAIL + 1)); fi
if printf '%s\n' "$probe" | grep -q '^METRICS 200'; then ok "llama.cpp /metrics is enabled"; PASS=$((PASS + 1)); else soft "llama.cpp /metrics not available (--metrics rejected?). The lab does not depend on it; set llm.metrics=false in the chart to silence the scrape target."; fi

bold "4/6  Gateway end-to-end (from your machine)"
resp="$(curl -sS -m 120 -N -X POST "http://127.0.0.1:${GATEWAY_PORT}/v1/generate" -H 'content-type: application/json' \
  -d '{"prompt":"What is an SLO?","max_tokens":8,"prompt_class":"short"}' 2>&1 || true)"
last="$(printf '%s\n' "$resp" | tail -n1)"
printf '    %s\n' "$last"
if printf '%s' "$last" | jq -e '.type == "done" and .status == "ok" and (.ttft_s != null)' >/dev/null 2>&1; then
  ok "request completed: status=ok ttft=$(printf '%s' "$last" | jq -r '.ttft_s | tostring | .[0:6]')s"; PASS=$((PASS + 1))
else printf '  \033[31m✘ gateway did not return a "done/ok" record\033[0m\n'; FAIL=$((FAIL + 1)); fi

bold "5/6  Observability"
prom_query() { curl -fsS -m 15 -G "http://127.0.0.1:${PROM_PORT}/api/v1/query" --data-urlencode "query=$1"; }
has_series() { prom_query "$1" | jq -e '.data.result | length > 0'; }
targets_up() {
  curl -fsS -m 15 "http://127.0.0.1:${PROM_PORT}/api/v1/targets" \
    | jq -e --arg ns "$NS_APP" '[.data.activeTargets[] | select(.labels.namespace == $ns) | select(.health == "up")] | length >= 2'
}
check "Prometheus scrapes gateway + retriever (targets up)" retry 120 targets_up
check "metric slo_requests_total exists" retry 60 has_series 'slo_requests_total'
check "recording rule slo:burn_rate:request_30s is being evaluated" retry 150 has_series 'slo:burn_rate:request_30s'
check "kube-state-metrics sees llm replicas" retry 60 has_series 'kube_deployment_status_replicas_ready{namespace="slo-loop",deployment="llm"}'
check "cAdvisor reports llm CPU" retry 90 has_series 'container_cpu_usage_seconds_total{namespace="slo-loop",pod=~"llm-.*",container="llama"}'
dash() { curl -fsS -m 15 -u "admin:slo-loop-lab" "http://127.0.0.1:${GRAFANA_PORT}/api/search?query=SLO-Loop" | jq -e 'length >= 3'; }
check "Grafana has the 3 'SLO-Loop' dashboards (sidecar-provisioned: Service health, Incident view, Autoscaling experiment)" retry 150 dash
check "recording rule slo:target_ratio (the dashboards' SLO line) is being evaluated" retry 150 has_series 'slo:target_ratio'

bold "6/6  Server-side validation of every manifest (real CRD schemas)"
OVERLAY=()
[ -f "$ROOT/config/autoscaling.yaml" ] && OVERLAY=(-f "$ROOT/config/autoscaling.yaml")
check "chaos manifests pass API-server dry-run" k apply --dry-run=server -f "$ROOT/chaos/"
for p in static1 static2 static3 cpu queue burn; do
  render() { h template slo-loop "$ROOT/charts/slo-loop-app" -n "$NS_APP" -f "$ROOT/config/slo.yaml" "${OVERLAY[@]}" --set "autoscaling.policy=$p" | k -n "$NS_APP" apply --dry-run=server -f -; }
  check "chart renders + validates for policy '$p'" render
done

bold "Result"
printf '  passed: %s   failed: %s   warnings: %s\n' "$PASS" "$FAIL" "$WARNS"
if [ "$FAIL" -ne 0 ]; then diagnose; die "verification FAILED - see docs/TROUBLESHOOTING.md"; fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$ROOT/.lab-verified"
if grep -q 'calibrated: true' "$ROOT/config/load.yaml" 2>/dev/null && grep -q 'calibrated: true' "$ROOT/config/slo.yaml" 2>/dev/null; then
  ok "everything verified. Capacity and SLO thresholds are already calibrated on this machine: do NOT re-run them; continue with the experiments (make phaseA / make phaseB)."
else
  ok "everything verified. Next: make latency && make capacity && make calibrate"
fi
