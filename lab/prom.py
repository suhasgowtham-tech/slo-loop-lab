"""Best-effort Prometheus snapshots (used for dashboards/cross-checks, never for ground truth)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .config import NAMESPACE, PROM_URL

SNAPSHOT_QUERIES = {
    "inflight": "sum(slo_inflight)",
    "burn_request_30s": "max(slo:burn_rate:request_30s)",
    "burn_token_30s": "max(slo:burn_rate:token_30s)",
    "llm_replicas_ready": f'kube_deployment_status_replicas_ready{{namespace="{NAMESPACE}",deployment="llm"}}',
    "ttft_p95_gateway": "histogram_quantile(0.95, sum by (le) (rate(slo_ttft_seconds_bucket[30s])))",
}


def snapshot(start_epoch: float, end_epoch: float, out_path: Path, step_s: int = 5) -> bool:
    data = {}
    try:
        with httpx.Client(base_url=PROM_URL, timeout=15.0) as c:
            for name, q in SNAPSHOT_QUERIES.items():
                r = c.get("/api/v1/query_range", params={"query": q, "start": start_epoch, "end": end_epoch, "step": step_s})
                r.raise_for_status()
                data[name] = r.json().get("data", {}).get("result", [])
        out_path.write_text(json.dumps(data), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 - snapshots are optional
        return False


def instant(query: str, timeout_s: float = 10.0) -> float | None:
    """Largest value of an instant query; 0.0 when it returns no series; None when Prometheus cannot be reached."""
    try:
        r = httpx.get(f"{PROM_URL}/api/v1/query", params={"query": query}, timeout=timeout_s)
        r.raise_for_status()
        vals = [float(x["value"][1]) for x in r.json().get("data", {}).get("result", []) if x.get("value")]
        vals = [v for v in vals if v == v]  # drop NaN
        return max(vals) if vals else 0.0
    except Exception:  # noqa: BLE001 - best effort
        return None
