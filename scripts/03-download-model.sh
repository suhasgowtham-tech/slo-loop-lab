#!/usr/bin/env bash
# Download the ~0.4 GB quantised model (Apache-2.0) and verify it. Re-runnable and resumable.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need curl; need sha256sum
bold "Model download"

REPO="${LAB_MODEL_REPO:-bartowski/Qwen2.5-0.5B-Instruct-GGUF}"
FILE="${LAB_MODEL_FILE:-Qwen2.5-0.5B-Instruct-Q4_K_M.gguf}"
URL="https://huggingface.co/${REPO}/resolve/main/${FILE}"
DEST="$ROOT/models/$FILE"
mkdir -p "$ROOT/models"

# For LFS files Hugging Face publishes the sha256 of the content in x-linked-etag.
expected="$(curl -fsSI "$URL" 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="x-linked-etag" {gsub(/"/, "", $2); print $2}' | head -n1 || true)"

verify() {
  [ -f "$DEST" ] || return 1
  [ "$(head -c 4 "$DEST")" = "GGUF" ] || { warn "file does not start with the GGUF magic bytes"; return 1; }
  [ "$(stat -c %s "$DEST")" -ge 300000000 ] || { warn "file is smaller than 300 MB"; return 1; }
  local got; got="$(sha256sum "$DEST" | cut -d' ' -f1)"
  if [ -n "$expected" ] && [ "${#expected}" -eq 64 ]; then
    [ "$got" = "$expected" ] || { warn "sha256 mismatch (got $got, expected $expected)"; return 1; }
    ok "sha256 matches Hugging Face's published value"
  else
    warn "no published sha256 available; trusting on first use and recording it"
  fi
  printf '%s  %s\n' "$got" "$FILE" > "$ROOT/models/MODEL.sha256"
}

if verify; then ok "model present and verified: $DEST"; exit 0; fi
[ -f "$DEST" ] && warn "existing file failed verification; resuming/re-downloading"
curl -fL -C - --retry 5 --retry-delay 3 --progress-bar -o "$DEST" "$URL"
verify || die "downloaded model failed verification - delete models/$FILE and retry, or check your connection"
ok "model ready: $DEST"
