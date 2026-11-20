#!/usr/bin/env bash
# Remove the kind cluster. Keeps ./models and ./results (your downloaded model and your data).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need kind
bold "Tearing down cluster '$CLUSTER'"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then kind delete cluster --name "$CLUSTER"; ok "cluster deleted"; else ok "cluster not present"; fi
rm -f "$ROOT/platform/.kind-config.generated.yaml" "$ROOT/.lab-verified"
