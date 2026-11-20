"""SLO-Loop gateway.

Front door of the lab workload: retrieve context -> stream a completion from llama.cpp.
It is instrumented so that every request becomes an SLI event (good / bad) in Prometheus,
and every response ends with a `done` line carrying per-stage timings that the load
generator logs as ground truth.

Definitions (keep in sync with docs/DESIGN.md and lab/loadgen.py):
  * TTFT   = time from request receipt to the first chunk received from the LLM
             (includes retrieval, queueing inside llama.cpp and prompt evaluation).
  * good   = status "ok" AND TTFT <= SLO threshold of the request's prompt class.
  * weight = ceil(len(prompt)/4) + max_tokens   (a cost proxy known at admission time).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

LLM_URL = os.getenv("LLM_URL", "http://llm:8081").rstrip("/")
RETRIEVER_URL = os.getenv("RETRIEVER_URL", "http://retriever:8001").rstrip("/")
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "60"))
RETRIEVE_TIMEOUT_S = float(os.getenv("RETRIEVE_TIMEOUT_S", "10"))
SLO_TTFT = {
    "short": float(os.getenv("SLO_TTFT_SHORT_S", "2")),
    "medium": float(os.getenv("SLO_TTFT_MEDIUM_S", "4")),
    "long": float(os.getenv("SLO_TTFT_LONG_S", "10")),
}
CLASSES = tuple(SLO_TTFT)

BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 60, 90)

REQS = Counter("slo_requests_total", "Completed requests by outcome", ["class", "status"])
GOOD = Counter("slo_good_requests_total", "Requests meeting the TTFT SLO", ["class"])
WEIGHT = Counter("slo_weight_total", "Cost-weighted request volume (token proxy)", ["class"])
GOOD_WEIGHT = Counter("slo_good_weight_total", "Cost-weighted volume of good requests", ["class"])
TOKENS = Counter("slo_tokens_total", "Tokens reported by the LLM", ["class", "kind"])
TTFT = Histogram("slo_ttft_seconds", "Time to first token (successful first chunk only)", ["class"], buckets=BUCKETS)
DURATION = Histogram("slo_duration_seconds", "Total request duration", ["class"], buckets=BUCKETS)
STAGE = Histogram("slo_stage_seconds", "Per-stage latency", ["stage"], buckets=BUCKETS)
INFLIGHT = Gauge("slo_inflight", "Requests admitted and not yet finished")

for _c in CLASSES:  # pre-create series so rate() works from the first scrape
    GOOD.labels(_c)
    WEIGHT.labels(_c)
    GOOD_WEIGHT.labels(_c)
    REQS.labels(_c, "ok")
    for _k in ("prompt", "completion"):
        TOKENS.labels(_c, _k)


def weight_of(prompt: str, max_tokens: int) -> int:
    return math.ceil(len(prompt) / 4) + max_tokens


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    max_tokens: int = Field(default=64, ge=1, le=512)
    prompt_class: Literal["short", "medium", "long"] = "medium"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # New TCP connection per LLM request => kube-proxy balances per request, not per pod pair.
    app.state.llm = httpx.AsyncClient(
        base_url=LLM_URL,
        timeout=None,
        limits=httpx.Limits(max_connections=2000, max_keepalive_connections=0),
    )
    app.state.retriever = httpx.AsyncClient(
        base_url=RETRIEVER_URL,
        timeout=RETRIEVE_TIMEOUT_S,
        limits=httpx.Limits(max_connections=500),
    )
    yield
    await app.state.llm.aclose()
    await app.state.retriever.aclose()


app = FastAPI(title="slo-loop-gateway", lifespan=lifespan)


def record(cls, status, weight, ttft_s, total_s, retrieve_s, llm_ttft_s, prompt_tokens, completion_tokens):
    REQS.labels(cls, status).inc()
    WEIGHT.labels(cls).inc(weight)
    if status == "ok" and ttft_s is not None and ttft_s <= SLO_TTFT[cls]:
        GOOD.labels(cls).inc()
        GOOD_WEIGHT.labels(cls).inc(weight)
    DURATION.labels(cls).observe(total_s)
    if ttft_s is not None:
        TTFT.labels(cls).observe(ttft_s)
    if retrieve_s is not None:
        STAGE.labels("retrieve").observe(retrieve_s)
    if llm_ttft_s is not None:
        STAGE.labels("llm_ttft").observe(llm_ttft_s)
    if prompt_tokens:
        TOKENS.labels(cls, "prompt").inc(prompt_tokens)
    if completion_tokens:
        TOKENS.labels(cls, "completion").inc(completion_tokens)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/admin/retriever-fault")
async def set_retriever_fault(request: Request):
    body = await request.json()
    r = await request.app.state.retriever.post("/admin/fault", json=body)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/admin/retriever-fault")
async def clear_retriever_fault(request: Request):
    r = await request.app.state.retriever.delete("/admin/fault")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/generate")
async def generate(req: GenerateRequest, request: Request):
    cls = req.prompt_class
    t0 = time.perf_counter()
    weight = weight_of(req.prompt, req.max_tokens)
    INFLIGHT.inc()

    # ---- stage 1: retrieval (a failure here is a real HTTP error, before any streaming)
    t_r = time.perf_counter()
    try:
        r = await request.app.state.retriever.post(
            "/retrieve", json={"query": " ".join(req.prompt.split()[:32])}
        )
        r.raise_for_status()
        snippets = r.json().get("snippets", [])
    except (httpx.HTTPError, ValueError):
        retrieve_s = time.perf_counter() - t_r
        record(cls, "retriever_error", weight, None, time.perf_counter() - t0, retrieve_s, None, 0, 0)
        INFLIGHT.dec()
        return JSONResponse(status_code=503, content={"status": "retriever_error"})
    retrieve_s = time.perf_counter() - t_r

    full_prompt = "Context:\n" + "\n".join(snippets) + "\n\nUser:\n" + req.prompt + "\n\nAnswer:"
    payload = {
        "prompt": full_prompt,
        "n_predict": req.max_tokens,
        "stream": True,
        "cache_prompt": False,  # otherwise repeated prefixes are served from cache and distort TTFT
        "ignore_eos": True,  # fixed output length => lower variance, controlled experiments
        "temperature": 0.8,
    }
    llm = request.app.state.llm
    deadline = t0 + REQUEST_TIMEOUT_S

    async def stream():
        status = "ok"
        recorded = False
        ttft_s = None
        llm_ttft_s = None
        prompt_tokens = 0
        completion_tokens = 0
        t_llm = time.perf_counter()
        saw_stop = False
        try:
            try:
                async with asyncio.timeout(max(0.1, deadline - time.perf_counter())):
                    async with llm.stream("POST", "/completion", json=payload) as resp:
                        if resp.status_code != 200:
                            status = f"llm_http_{resp.status_code}"
                        else:
                            async for raw in resp.aiter_lines():
                                line = raw.strip()
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    line = line[5:].strip()
                                if line == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(line)
                                except ValueError:
                                    continue
                                if not isinstance(chunk, dict):
                                    continue
                                if "error" in chunk:
                                    status = "llm_error"
                                    break
                                now = time.perf_counter()
                                if ttft_s is None:
                                    ttft_s = now - t0
                                    llm_ttft_s = now - t_llm
                                content = chunk.get("content") or ""
                                if content:
                                    completion_tokens += 1
                                    yield json.dumps({"type": "token", "text": content}) + "\n"
                                if chunk.get("stop") or chunk.get("stopped_limit") or chunk.get("stopped_eos"):
                                    saw_stop = True
                                    prompt_tokens = int(chunk.get("tokens_evaluated") or 0)
                                    completion_tokens = int(chunk.get("tokens_predicted") or completion_tokens)
                                    break
                            if status == "ok" and not saw_stop:
                                status = "llm_incomplete"
            except TimeoutError:
                status = "timeout"
            except httpx.HTTPError:
                status = "llm_error"

            total_s = time.perf_counter() - t0
            record(cls, status, weight, ttft_s, total_s, retrieve_s, llm_ttft_s, prompt_tokens, completion_tokens)
            recorded = True
            yield json.dumps(
                {
                    "type": "done",
                    "status": status,
                    "ttft_s": ttft_s,
                    "total_s": total_s,
                    "retrieve_s": retrieve_s,
                    "llm_ttft_s": llm_ttft_s,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
            ) + "\n"
        finally:
            INFLIGHT.dec()
            if not recorded:  # client went away mid-stream
                record(cls, "client_abort", weight, ttft_s, time.perf_counter() - t0, retrieve_s, llm_ttft_s, 0, 0)

    return StreamingResponse(stream(), media_type="application/x-ndjson")
