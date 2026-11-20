#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need kind; need kubectl; need docker
bold "Creating kind cluster '$CLUSTER'"
rm -f "$ROOT/.lab-verified"   # state is changing: verification no longer holds

MODELS_DIR="$ROOT/models"
mkdir -p "$MODELS_DIR"
GEN="$ROOT/platform/.kind-config.generated.yaml"
sed -e "s|__MODELS_DIR__|${MODELS_DIR}|g" -e "s|__GATEWAY_PORT__|${GATEWAY_PORT}|g" -e "s|__PROM_PORT__|${PROM_PORT}|g" -e "s|__GRAFANA_PORT__|${GRAFANA_PORT}|g" "$ROOT/platform/kind-config.yaml.tmpl" > "$GEN"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  ok "cluster '$CLUSTER' already exists"
else
  kind create cluster --name "$CLUSTER" --config "$GEN" --wait 180s
fi

k wait --for=condition=Ready node --all --timeout=180s >/dev/null
ok "node Ready (context: $KCTX)"

k create namespace "$NS_APP" --dry-run=client -o yaml | k apply -f - >/dev/null
# Chaos Mesh honours experiments only in annotated namespaces (see platform/values/chaos-mesh.yaml).
k annotate namespace "$NS_APP" chaos-mesh.org/inject=enabled --overwrite >/dev/null
ok "namespace $NS_APP ready and annotated for chaos"

node="${CLUSTER}-control-plane"
if docker exec "$node" ls /models >/dev/null 2>&1; then ok "models directory is mounted into the node"; else die "models mount missing inside $node (is the repo on the Linux filesystem?)"; fi
node_img="$(docker inspect --format '{{.Config.Image}}' "$node" 2>/dev/null || true)"
if [ -n "$node_img" ]; then ok "node image: $node_img"; fi
