#!/usr/bin/env bash
# Install kind, kubectl and Helm 3 into ~/.local/bin (no sudo). Latest stable versions are resolved at
# install time, integrity-checked where the publisher provides checksums, and recorded in
# platform/versions.lock so you can see exactly what produced your results.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need curl; need jq; need sha256sum; need tar

BIN="${LAB_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"
case "$(uname -m)" in
  x86_64) A=amd64 ;;
  aarch64 | arm64) A=arm64 ;;
  *) die "unsupported CPU architecture: $(uname -m)" ;;
esac
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOCK="$ROOT/platform/versions.lock"
touch "$LOCK"

record() { # key value
  grep -v "^$1=" "$LOCK" > "$LOCK.tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$LOCK.tmp"
  sort "$LOCK.tmp" > "$LOCK"
  rm -f "$LOCK.tmp"
}

install_kind() {
  if command -v kind >/dev/null 2>&1 && [ -z "${LAB_FORCE_TOOLS:-}" ]; then ok "kind already installed: $(kind version)"; record tool.kind "$(kind version | awk '{print $2}')"; return; fi
  bold "Installing kind"
  local tag url
  tag=$(curl -fsSL https://api.github.com/repos/kubernetes-sigs/kind/releases/latest | jq -r .tag_name)
  [ -n "$tag" ] && [ "$tag" != null ] || die "could not resolve latest kind release (GitHub API rate limit? retry in a few minutes)"
  url="https://github.com/kubernetes-sigs/kind/releases/download/${tag}/kind-linux-${A}"
  curl -fsSL -o "$TMP/kind" "$url"
  if curl -fsSL -o "$TMP/kind.sha256sum" "${url}.sha256sum" 2>/dev/null; then
    (cd "$TMP" && echo "$(cut -d' ' -f1 kind.sha256sum)  kind" | sha256sum -c - >/dev/null) || die "kind checksum MISMATCH - aborting"
    ok "kind checksum verified"
  else
    warn "no published checksum for kind ${tag}; downloaded over TLS from github.com"
  fi
  install -m 0755 "$TMP/kind" "$BIN/kind"
  ok "kind ${tag} -> $BIN/kind"; record tool.kind "$tag"
}

install_kubectl() {
  if command -v kubectl >/dev/null 2>&1 && [ -z "${LAB_FORCE_TOOLS:-}" ]; then ok "kubectl already installed"; record tool.kubectl "$(kubectl version --client -o json 2>/dev/null | jq -r .clientVersion.gitVersion)"; return; fi
  bold "Installing kubectl"
  local v
  v=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
  [ -n "$v" ] || die "could not resolve latest kubectl version"
  curl -fsSL -o "$TMP/kubectl" "https://dl.k8s.io/release/${v}/bin/linux/${A}/kubectl"
  curl -fsSL -o "$TMP/kubectl.sha256" "https://dl.k8s.io/release/${v}/bin/linux/${A}/kubectl.sha256"
  (cd "$TMP" && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - >/dev/null) || die "kubectl checksum MISMATCH - aborting"
  ok "kubectl checksum verified"
  install -m 0755 "$TMP/kubectl" "$BIN/kubectl"
  ok "kubectl ${v} -> $BIN/kubectl"; record tool.kubectl "$v"
}

install_helm() {
  if command -v helm >/dev/null 2>&1 && [ -z "${LAB_FORCE_TOOLS:-}" ]; then
    case "$(helm version --short 2>/dev/null)" in
      v3.*) ok "helm already installed: $(helm version --short)"; record tool.helm "$(helm version --short)"; return ;;
      *) warn "installed helm is not 3.x ($(helm version --short 2>/dev/null)); installing Helm 3 alongside it in $BIN (this lab's scripts target Helm 3 semantics)" ;;
    esac
  fi
  bold "Installing Helm 3"
  local tag
  tag=$(curl -fsSL "https://api.github.com/repos/helm/helm/releases?per_page=50" | jq -r '[.[] | select(.prerelease == false) | select(.tag_name | startswith("v3."))][0].tag_name')
  [ -n "$tag" ] && [ "$tag" != null ] || die "could not resolve latest Helm 3 release"
  curl -fsSL -o "$TMP/helm.tgz" "https://get.helm.sh/helm-${tag}-linux-${A}.tar.gz"
  curl -fsSL -o "$TMP/helm.tgz.sha256sum" "https://get.helm.sh/helm-${tag}-linux-${A}.tar.gz.sha256sum"
  (cd "$TMP" && echo "$(cut -d' ' -f1 helm.tgz.sha256sum)  helm.tgz" | sha256sum -c - >/dev/null) || die "helm checksum MISMATCH - aborting"
  ok "helm checksum verified"
  tar -xzf "$TMP/helm.tgz" -C "$TMP"
  install -m 0755 "$TMP/linux-${A}/helm" "$BIN/helm"
  ok "helm ${tag} -> $BIN/helm"; record tool.helm "$tag"
}

install_kind
install_kubectl
install_helm
case ":$PATH:" in *":$BIN:"*) ;; *) warn "add to ~/.bashrc:  export PATH=\"$BIN:\$PATH\"" ;; esac
ok "tools ready (versions recorded in platform/versions.lock)"
