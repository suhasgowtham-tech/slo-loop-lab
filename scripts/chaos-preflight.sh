#!/usr/bin/env bash
# Finds out which fault types THIS machine can really inject, and records it in config/capabilities.yaml.
# WSL2 kernels do not always ship netem; scenarios that need an unsupported fault are skipped, never faked.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need kubectl
require_cluster
bold "Chaos preflight"
CAP="$ROOT/config/capabilities.yaml"

cleanup() {
  for f in cpu-stress-llm pod-kill-llm netem-delay-llm; do
    k delete -f "$ROOT/chaos/$f.yaml" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
cleanup

injected() { # resource name -> wait up to 60 s for AllInjected=True
  local res="$1" name="$2" s
  for _ in $(seq 1 30); do
    s="$(k -n "$NS_APP" get "$res" "$name" -o jsonpath='{.status.conditions[?(@.type=="AllInjected")].status}' 2>/dev/null || true)"
    if [ "$s" = "True" ]; then return 0; fi
    sleep 2
  done
  return 1
}
lat_ms() { # fastest of 3 gateway->llm /health round trips, in ms
  local best=999999 v
  for _ in 1 2 3; do
    v="$(k -n "$NS_APP" exec deploy/gateway -c gateway -- python -c "import httpx,time;t=time.perf_counter();httpx.get('http://llm:8081/health',timeout=30);print(int((time.perf_counter()-t)*1000))" 2>/dev/null || echo 999999)"
    [ "$v" -lt "$best" ] && best="$v"
  done
  echo "$best"
}

stress=false; podkill=false; netem=false

k apply -f "$ROOT/chaos/cpu-stress-llm.yaml" >/dev/null
if injected stresschaos lab-cpu-stress-llm; then stress=true; ok "StressChaos injects"; else warn "StressChaos did not reach AllInjected"; fi
k delete -f "$ROOT/chaos/cpu-stress-llm.yaml" --wait=false >/dev/null 2>&1 || true

base="$(lat_ms)"
k apply -f "$ROOT/chaos/netem-delay-llm.yaml" >/dev/null
if injected networkchaos lab-netem-delay-llm; then
  sleep 3
  during="$(lat_ms)"
  if [ $((during - base)) -ge 300 ]; then netem=true; ok "NetworkChaos delay is REAL (llm /health ${base} ms -> ${during} ms)"
  else warn "NetworkChaos reported injected but latency did not change (${base} -> ${during} ms): kernel lacks netem. Scenario s6 will be skipped."; fi
else warn "NetworkChaos did not reach AllInjected. Scenario s6 will be skipped."; fi
k delete -f "$ROOT/chaos/netem-delay-llm.yaml" --wait=false >/dev/null 2>&1 || true

k apply -f "$ROOT/chaos/pod-kill-llm.yaml" >/dev/null
if injected podchaos lab-pod-kill-llm; then podkill=true; ok "PodChaos kills a pod"; else warn "PodChaos did not reach AllInjected"; fi
k delete -f "$ROOT/chaos/pod-kill-llm.yaml" --wait=false >/dev/null 2>&1 || true
k -n "$NS_APP" rollout status deploy/llm --timeout=240s >/dev/null || warn "llm did not recover within 240 s"

cat > "$CAP" <<EOF
# Written by scripts/chaos-preflight.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
stress: $stress
podkill: $podkill
netem: $netem
EOF
ok "wrote $CAP"
sed 's/^/    /' "$CAP"
[ "$stress" = true ] && [ "$podkill" = true ] || die "StressChaos/PodChaos are required for scenarios s3/s4; see docs/TROUBLESHOOTING.md (Chaos Mesh)"
