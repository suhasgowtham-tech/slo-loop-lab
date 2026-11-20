"""Runs INSIDE the gateway pod (see scripts/verify.sh).

Checks, against the real llama.cpp server, every assumption the lab makes about it:
  * GET /health answers 200 when ready
  * GET /metrics exists (needs --metrics) - prints the metric names it exposes
  * POST /completion with stream=true yields JSON chunks (SSE `data:` lines or raw NDJSON) and a
    final chunk with stop=true (+ token counts)
Output is machine-readable `KEY value...` lines that verify.sh greps.
"""
import json
import os

import httpx

URL = os.environ.get("LLM_URL", "http://llm:8081").rstrip("/")


def out(*a):
    print(*a, flush=True)


try:
    r = httpx.get(f"{URL}/health", timeout=10)
    out("HEALTH", r.status_code, r.text[:80].replace("\n", " "))
except Exception as e:  # noqa: BLE001
    out("HEALTH", "error", e)

try:
    m = httpx.get(f"{URL}/metrics", timeout=10)
    names = sorted({ln.split("{")[0].split(" ")[0] for ln in m.text.splitlines() if ln and not ln.startswith("#")})
    out("METRICS", m.status_code, ",".join(names) or "-")
except Exception as e:  # noqa: BLE001
    out("METRICS", "error", e)

final = None
n = 0
fmt = None
payload = {"prompt": "Say hello.", "n_predict": 4, "stream": True, "cache_prompt": False, "ignore_eos": True}
try:
    with httpx.stream("POST", f"{URL}/completion", json=payload, timeout=120) as r:
        out("COMPLETION", r.status_code)
        for line in r.iter_lines():
            if not line.strip():
                continue
            n += 1
            if fmt is None:
                fmt = "sse" if line.startswith("data:") else "ndjson"
            if n <= 2:
                out("RAW", line[:140])
            body = line[5:].strip() if line.startswith("data:") else line
            try:
                j = json.loads(body)
            except ValueError:
                continue
            if isinstance(j, dict):
                final = j
except Exception as e:  # noqa: BLE001
    out("COMPLETION", "error", e)

if isinstance(final, dict):
    keys = ("stop", "stopped_limit", "tokens_evaluated", "tokens_predicted")
    out("FINAL", json.dumps({k: final.get(k) for k in keys}), f"format={fmt}", f"chunks={n}")
else:
    out("FINAL", "none")
