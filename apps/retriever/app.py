"""SLO-Loop retriever: tiny in-memory lexical retriever with controllable fault injection.

The fault endpoints exist so experiments can inject *application-level* faults (latency,
errors) that do not depend on any kernel feature (netem etc.). They are reachable through
the gateway's /admin/retriever-fault proxy; the retriever Service itself is ClusterIP-only.
"""
from __future__ import annotations

import asyncio
import os
import random
import time

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

CORPUS_SIZE = int(os.getenv("CORPUS_SIZE", "500"))
TOP_K = int(os.getenv("TOP_K", "1"))
SNIPPET_WORDS = int(os.getenv("SNIPPET_WORDS", "6"))

# Real words, not "term123": a tokenizer splits digits one by one, so "term123" costs 4 tokens and the retrieved
# context used to add ~64 prompt tokens (~1 s of CPU prefill) to EVERY request. The context must stay small and
# constant so that request cost is dominated by the prompt class the experiment controls.
_rng = random.Random(1234)
_VOCAB = (
    "latency queue budget replica burn alert window cache token model cluster node probe rollout "
    "capacity traffic timeout retry error signal metric scrape deploy scale limit quota policy "
    "service gateway backend stream prompt context answer incident review recover inject fault"
).split()
DOCS = [" ".join(_rng.choice(_VOCAB) for _ in range(SNIPPET_WORDS)) for _ in range(CORPUS_SIZE)]
DOC_SETS = [set(d.split()) for d in DOCS]

FAULT = {"latency_ms": 0.0, "jitter_ms": 0.0, "error_rate": 0.0}
_fault_rng = random.Random(99)

REQS = Counter("retriever_requests_total", "Retrieval requests", ["status"])
DUR = Histogram(
    "retriever_duration_seconds",
    "Retrieval handler duration (includes injected latency)",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8),
)
REQS.labels("ok")
REQS.labels("error")

app = FastAPI(title="slo-loop-retriever")


class Query(BaseModel):
    query: str = ""


class FaultSpec(BaseModel):
    latency_ms: float = Field(default=0.0, ge=0, le=60000)
    jitter_ms: float = Field(default=0.0, ge=0, le=60000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/admin/fault")
async def get_fault():
    return FAULT


@app.post("/admin/fault")
async def set_fault(spec: FaultSpec):
    FAULT.update(spec.model_dump())
    return FAULT


@app.delete("/admin/fault")
async def clear_fault():
    FAULT.update({"latency_ms": 0.0, "jitter_ms": 0.0, "error_rate": 0.0})
    return FAULT


@app.post("/retrieve")
async def retrieve(q: Query):
    t0 = time.perf_counter()
    try:
        delay = FAULT["latency_ms"] + (_fault_rng.uniform(-1, 1) * FAULT["jitter_ms"] if FAULT["jitter_ms"] else 0.0)
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)
        if FAULT["error_rate"] > 0 and _fault_rng.random() < FAULT["error_rate"]:
            REQS.labels("error").inc()
            return JSONResponse(status_code=503, content={"error": "injected"})
        qs = set(q.query.split())
        scored = sorted(range(len(DOCS)), key=lambda i: -len(qs & DOC_SETS[i]))[:TOP_K]
        REQS.labels("ok").inc()
        return {"snippets": [DOCS[i] for i in scored]}
    finally:
        DUR.observe(time.perf_counter() - t0)
