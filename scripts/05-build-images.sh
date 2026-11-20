#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker; need kind
bold "Building application images"
TAG="0.2.0"
# Docker Desktop's containerd image store + BuildKit attach provenance/SBOM attestations, which makes
# `kind load docker-image` fail ("content digest ... not found"). Switch them off when supported.
flags=()
if docker build --help 2>/dev/null | grep -q -- '--provenance'; then flags+=(--provenance=false --sbom=false); fi
for app in gateway retriever; do
  docker build "${flags[@]}" -q -t "slo-loop/${app}:${TAG}" "$ROOT/apps/${app}" >/dev/null
  ok "built slo-loop/${app}:${TAG}"
  kind load docker-image "slo-loop/${app}:${TAG}" --name "$CLUSTER" >/dev/null
  ok "loaded slo-loop/${app}:${TAG} into kind"
done
