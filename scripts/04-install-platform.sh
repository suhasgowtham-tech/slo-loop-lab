#!/usr/bin/env bash
# Install metrics-server, kube-prometheus-stack (release "kps"), KEDA and Chaos Mesh.
# First run installs the latest chart versions and records them in platform/versions.lock;
# later runs install exactly the locked versions (delete the chart.* lines to upgrade).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need helm; need kubectl; need jq
require_cluster
LOCK="$ROOT/platform/versions.lock"
touch "$LOCK"

bold "Helm repositories"
h repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
h repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
h repo add chaos-mesh https://charts.chaos-mesh.org --force-update >/dev/null
h repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update >/dev/null
h repo update >/dev/null
ok "repositories ready"

locked() { awk -F= -v k="chart.$1" '$1 == k {print $2}' "$LOCK"; }

install() { # release chart namespace [values-file]
  local rel="$1" chart="$2" ns="$3" vals="${4:-}" ver
  ver="$(locked "$rel")"
  local args=(upgrade --install "$rel" "$chart" -n "$ns" --create-namespace)
  if [ -n "$vals" ]; then args+=(-f "$vals"); fi
  if [ -n "$ver" ]; then args+=(--version "$ver"); fi
  args+=(--wait --timeout 10m)
  bold "Installing $rel ($chart${ver:+ @ $ver})"
  if ! h "${args[@]}"; then
    warn "install of $rel failed; pod status in $ns:"
    k -n "$ns" get pods 2>&1 | sed 's/^/    /' || true
    die "$rel install failed (see docs/TROUBLESHOOTING.md)"
  fi
  ok "$rel installed"
}

install metrics-server metrics-server/metrics-server kube-system "$ROOT/platform/values/metrics-server.yaml"
install kps prometheus-community/kube-prometheus-stack monitoring "$ROOT/platform/values/kube-prometheus-stack.yaml"
install keda kedacore/keda keda
install chaos-mesh chaos-mesh/chaos-mesh chaos-mesh "$ROOT/platform/values/chaos-mesh.yaml"

bold "Recording chart versions"
# `helm list` gives "<chart>-<version>"; strip everything up to the last dash that precedes a digit.
h list -A -o json \
  | jq -r '.[] | select(.name == "metrics-server" or .name == "kps" or .name == "keda" or .name == "chaos-mesh") | "chart.\(.name)=\(.chart)"' \
  | sed -E 's/^(chart\.[^=]+)=.*-([0-9].*)$/\1=\2/' > "$LOCK.new"
{ grep -v '^chart\.' "$LOCK" || true; cat "$LOCK.new"; } | sort > "$LOCK.tmp"
mv "$LOCK.tmp" "$LOCK"
rm -f "$LOCK.new"
sed 's/^/    /' "$LOCK"
ok "platform ready"
