"""A stand-in for llama.cpp's server used ONLY by the local test-suite.

Mimics the parts of the native API the gateway relies on: GET /health and a streaming
POST /completion (SSE "data: {...}" lines, final chunk with stop/tokens_evaluated/tokens_predicted).
It has N parallel slots so queueing behaves like the real thing. The streaming format here is
my reconstruction of llama.cpp's behaviour, NOT a copy of its code: `make verify` probes the
real server for exactly this reason.
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

SLOTS = int(os.getenv("FAKE_SLOTS", "2"))
PREFILL_S_PER_1K_CHARS = float(os.getenv("FAKE_PREFILL_S_PER_1K_CHARS", "0.05"))
BASE_FIRST_TOKEN_S = float(os.getenv("FAKE_FIRST_TOKEN_S", "0.02"))
TOKEN_S = float(os.getenv("FAKE_TOKEN_S", "0.005"))

app = FastAPI()
_sem: asyncio.Semaphore | None = None


def sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(SLOTS)
    return _sem


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.post("/completion")
async def completion(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    n = int(body.get("n_predict", 16))

    async def gen():
        async with sem():
            await asyncio.sleep(BASE_FIRST_TOKEN_S + PREFILL_S_PER_1K_CHARS * len(prompt) / 1000.0)
            for i in range(n):
                if i:
                    await asyncio.sleep(TOKEN_S)
                yield "data: " + json.dumps({"content": " tok", "stop": False}) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "content": "",
                    "stop": True,
                    "tokens_evaluated": max(1, len(prompt) // 4),
                    "tokens_predicted": n,
                }
            ) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
