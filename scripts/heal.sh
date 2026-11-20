#!/usr/bin/env bash
# Repair the lab after a Windows / WSL / Docker Desktop restart. Safe to run any time; changes nothing when healthy.
#   1. the kind node must be running
#   2. the model must be visible INSIDE the node (/models). If the bind mount came back empty, restarting the node
#      container makes Docker re-create it from the now-running Ubuntu distro.
#   3. pods are restarted so no container sits in a long CrashLoopBackOff timer.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need kubectl; require_docker
bold "Heal after a restart"
WAIT_S="${HEAL_MOUNT_WAIT_S:-45}"

docker inspect "$NODE" >/dev/null 2>&1 || die "kind node '$NODE' does not exist: the cluster is gone. Run: make up"
[ -s "$ROOT/models/$MODEL_FILE" ] || die "model file is missing on disk ($ROOT/models/$MODEL_FILE). Run: make model"
ok "model file present on disk"
docker start "$NODE" >/dev/null 2>&1 || true

wait_node_ready() {
  for _ in $(seq 1 100); do
    if k wait --for=condition=Ready node --all --timeout=5s >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

if node_has_model; then
  ok "model is visible inside the kind node"
else
  warn "model is NOT visible inside $NODE:/models (stale bind mount after a restart). Restarting the node container ..."
  docker restart "$NODE" >/dev/null
  wait_node_ready || die "node did not become Ready within 5 minutes. Check: docker ps ; docker logs $NODE --tail 30"
  end=$((SECONDS + WAIT_S))
  while [ "$SECONDS" -lt "$end" ] && ! node_has_model; do sleep 2; done
  node_has_model || die "model is STILL not visible inside the node. Recreate the cluster (the model download and your results are kept): make down && make up"
  ok "model is visible inside the kind node again"
fi

wait_node_ready || die "node is not Ready"
ok "node Ready"

if h status slo-loop -n "$NS_APP" >/dev/null 2>&1; then
  k -n "$NS_APP" rollout restart deploy/llm deploy/gateway deploy/retriever >/dev/null
  for d in llm gateway retriever; do
    k -n "$NS_APP" rollout status "deploy/$d" --timeout=300s >/dev/null || { diagnose; die "deploy/$d did not become ready"; }
  done
  ok "llm, gateway and retriever are ready"
else
  warn "release 'slo-loop' is not deployed yet: continue with  make deploy"
fi
rm -f "$ROOT/.lab-verified"
ok "healed. Next: make deploy && make verify   (then make latency)"
