#!/usr/bin/env bash
# Pin the llama.cpp image by digest so every run (and any result you publish) used byte-identical bits.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker
IMG="${LAB_LLM_IMAGE:-ghcr.io/ggml-org/llama.cpp:server}"
bold "Pinning $IMG"
docker pull "$IMG" >/dev/null
digest="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMG" | sed 's/.*@//')"
case "$digest" in sha256:*) ;; *) die "could not read a repo digest for $IMG" ;; esac
cat > "$ROOT/config/images.yaml" <<EOF
# Written by scripts/pin-images.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Delete this file to float back to :server.
images:
  llm:
    digest: "$digest"
EOF
ok "pinned llm image to $digest (config/images.yaml)"
printf '  Re-deploy to apply:  make deploy\n'
