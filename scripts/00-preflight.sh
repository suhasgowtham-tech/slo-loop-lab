#!/usr/bin/env bash
# Fail fast, with the exact fix, before anything is installed.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
bold "Preflight"
fail=0
bad() { printf '  \033[31m✘ %s\033[0m\n' "$*" >&2; fail=1; }

if grep -qi microsoft /proc/version 2>/dev/null; then ok "running inside WSL2"; else warn "not WSL2 (fine on native Linux)"; fi

case "$ROOT" in
  /mnt/*) bad "repo is on the Windows drive ($ROOT). Move it into the Linux filesystem, e.g. ~/slo-loop-lab (bind mounts and file I/O are broken/slow on /mnt/*)." ;;
  *) ok "repo is on the Linux filesystem ($ROOT)" ;;
esac

docker_ok=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "docker daemon reachable"; docker_ok=1
else
  bad "docker daemon not reachable. Start Docker Desktop, then Settings > Resources > WSL integration > enable your distro."
fi

# `docker build` shells out to the credential helper named in ~/.docker/config.json. Docker Desktop writes
# "credsStore": "desktop.exe", a Windows helper that WSL often cannot execute -> every image build fails.
cfgfile="$HOME/.docker/config.json"
if [ -f "$cfgfile" ] && command -v jq >/dev/null 2>&1; then
  store="$(jq -r '.credsStore // empty' "$cfgfile" 2>/dev/null || true)"
  if [ -n "$store" ]; then
    if command -v "docker-credential-$store" >/dev/null 2>&1; then ok "docker credential helper '$store' is runnable"
    else bad "docker credential helper 'docker-credential-$store' is not runnable from WSL (image builds will fail). Fix: cp ~/.docker/config.json ~/.docker/config.json.bak && jq 'del(.credsStore)' ~/.docker/config.json.bak > ~/.docker/config.json"; fi
  else ok "docker credential store: none configured (good for WSL)"; fi
fi

mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
if [ "$mem_mb" -ge 11000 ]; then ok "memory visible to WSL: ${mem_mb} MB"; else
  bad "only ${mem_mb} MB visible to WSL (need >= 11000). Put 'memory=12GB' in %UserProfile%\\.wslconfig (see docs/wslconfig.example) then run 'wsl --shutdown' in PowerShell."
fi

cpus=$(nproc)
if [ "$cpus" -ge 6 ]; then ok "logical CPUs: $cpus"; elif [ "$cpus" -ge 4 ]; then
  warn "only $cpus logical CPUs: the 3-replica policies will oversubscribe the CPU; results are valid only for relative comparison. Prefer >= 6."
else bad "only $cpus logical CPUs (need >= 4)."; fi

free_gb=$(df -Pk "$ROOT" | awk 'NR==2 {printf "%d", $4/1024/1024}')
if [ "$free_gb" -ge 25 ]; then ok "free disk: ${free_gb} GB"; else bad "only ${free_gb} GB free (need >= 25: images ~6 GB, model 0.4 GB, Prometheus data, results)."; fi

# Background load distorts every latency number. 1-minute load average well above 0 at idle = something is running.
load1="$(awk '{print $1}' /proc/loadavg)"
if awk -v l="$load1" -v c="$cpus" 'BEGIN { exit !(l > 0.25 * c) }'; then
  warn "load average is ${load1} on ${cpus} CPUs before the lab runs. Close browsers/IDEs/other containers; measurements need a quiet machine."
else ok "machine is quiet (load average ${load1})"; fi

# Docker Desktop's own Kubernetes (Settings > Kubernetes) is a second cluster that eats RAM + CPU for nothing.
if [ "$docker_ok" = 1 ]; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eq '^k8s_|^desktop-control-plane$|^desktop-worker'; then
    warn "Docker Desktop's built-in Kubernetes is running. The lab uses kind instead: Docker Desktop > Settings > Kubernetes > untick 'Enable Kubernetes' > Apply."
  else ok "Docker Desktop's built-in Kubernetes is not running"; fi
fi

# Every other running container (other kind clusters, other labs) competes for the same CPUs.
if [ "$docker_ok" = 1 ]; then
  others="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -v "^${CLUSTER}-control-plane$" | tr '\n' ' ' || true)"
  if [ -n "${others// /}" ]; then
    warn "other containers are running: ${others}. Each idle Kubernetes control plane costs 0.3-0.5 CPU and they distort every measurement. Stop what you do not need: docker stop ${others}  (reversible: docker start ...)"
  else ok "no other containers are running"; fi
fi

# Ports. If the kind cluster already exists it is the one holding them, so only a FIRST creation needs them free.
cluster_exists=0
if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then cluster_exists=1; fi
if [ "$cluster_exists" = 1 ]; then
  ok "kind cluster '$CLUSTER' already exists (ports are held by the cluster itself; skipping port checks)"
  if [ "$docker_ok" = 1 ] && [ -s "$ROOT/models/$MODEL_FILE" ]; then
    if node_has_model; then ok "model is visible inside the kind node"
    else bad "the model is on disk but NOT visible inside the kind node (stale bind mount after a Windows/WSL/Docker restart). Fix: make heal"; fi
  fi
else
  for p in "$GATEWAY_PORT" "$PROM_PORT" "$GRAFANA_PORT"; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then
      bad "port $p is already in use on localhost (kind needs it). Stop that process, or pick another port: export GATEWAY_PORT=18080 PROM_PORT=19090 GRAFANA_PORT=13000 (add to ~/.bashrc)."
    else ok "port $p is free (WSL side)"; fi
  done
  # Docker Desktop publishes container ports on the WINDOWS side, so a Windows program on the port breaks `kind create cluster`.
  if command -v powershell.exe >/dev/null 2>&1; then
    winbusy="$(timeout 20 powershell.exe -NoProfile -Command "foreach(\$p in $GATEWAY_PORT,$PROM_PORT,$GRAFANA_PORT){ if(Get-NetTCPConnection -State Listen -LocalPort \$p -ErrorAction SilentlyContinue){ \$p } }" 2>/dev/null | tr -d '\r' | tr '\n' ' ' || true)"
    if [ -n "${winbusy// /}" ]; then bad "Windows is already listening on port(s): $winbusy. Choose other ports (see above)."; else ok "ports are free on the Windows side too"; fi
  else warn "powershell.exe not reachable from WSL: could not check the Windows side of the ports (only matters if 'make cluster' fails with 'ports are not available')."; fi
fi

missing=""
for t in kind kubectl helm curl jq python3; do command -v "$t" >/dev/null 2>&1 || missing="$missing $t"; done
if [ -n "$missing" ]; then warn "missing tools:$missing  -> run: make tools"; else ok "tools present: kind kubectl helm curl jq python3"; fi
# A Windows kubectl/helm (from /mnt/c) earlier on PATH than the Linux one is slow and breaks paths. lib.sh puts ~/.local/bin first.
for t in kind kubectl helm; do
  p="$(command -v "$t" 2>/dev/null || true)"
  case "$p" in /mnt/*) warn "$t resolves to a Windows binary ($p). Use the Linux one: make tools" ;; esac
done

py_ok=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))' 2>/dev/null || echo 0)
if [ "$py_ok" = "1" ]; then ok "python3 >= 3.10"; else bad "python3 >= 3.10 required (Ubuntu 22.04+ ships it)."; fi

if [ "$fail" -ne 0 ]; then die "preflight failed - fix the items above and re-run: make preflight"; fi
ok "preflight passed"
