"""Grafana markers for injected faults and experiment runs.

The harness posts a region annotation to Grafana for every fault it applies and for every run it measures, so every
time-series panel shows "fault on" as a shaded band with its name on hover (the dashboards read them by tag:
`slo-loop` + `fault` / `run`, see lab/dashgen.py).

Hard rules, because the harness is a measuring instrument and a dashboard is not allowed to disturb it:
  * never raises into a run and never blocks it: every HTTP call happens on one background thread with a short timeout;
  * after two failures in a row it stops trying (a missing Grafana costs a run at most 2 x timeout of background time);
  * `LAB_ANNOTATE=0` switches it off entirely;
  * it only ever talks to the lab's own Grafana (LAB_GRAFANA_URL, 127.0.0.1 by default).
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field

import httpx

from . import config as cfg

TAG = "slo-loop"
MAX_CONSECUTIVE_FAILURES = 2


def enabled_by_env() -> bool:
    return os.getenv("LAB_ANNOTATE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _ms(epoch: float) -> int:
    return int(round(epoch * 1000))


@dataclass
class Marker:
    text: str
    tags: list[str]
    start: float
    id: int | None = None          # set by the worker when Grafana accepted the POST
    done: threading.Event = field(default_factory=threading.Event)


class Annotator:
    def __init__(self, base_url: str | None = None, auth: tuple[str, str] | None = None, *, enabled: bool | None = None,
                 timeout_s: float = 2.0, say=None):
        self.base_url = (base_url or cfg.GRAFANA_URL).rstrip("/")
        self.auth = auth or (cfg.GRAFANA_USER, cfg.GRAFANA_PASSWORD)
        self.enabled = enabled_by_env() if enabled is None else enabled
        self.timeout_s = timeout_s
        self.say = say or (lambda m: print(m, file=sys.stderr))
        self.failures = 0
        self.last_error: str | None = None
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._warned = False

    # ------------------------------------------------------------------ public, never raises
    def start(self, text: str, tags: list[str], at: float | None = None) -> Marker | None:
        """Open a marker now (or at `at`). Returns a handle for end(), or None when disabled."""
        if not self.enabled:
            return None
        m = Marker(text=text, tags=[TAG, *tags], start=time.time() if at is None else at)
        self._submit(("post", m, None))
        return m

    def end(self, marker: Marker | None, at: float | None = None) -> None:
        """Turn the point marker into a region that ends now (or at `at`)."""
        if marker is not None and self.enabled:
            self._submit(("put", marker, time.time() if at is None else at))

    def flush(self, timeout_s: float = 6.0) -> bool:
        """Wait (bounded) for queued calls to finish; True when the queue drained."""
        if not self.enabled or self._worker is None:
            return True
        done = threading.Event()
        self._q.put(("flush", done, None))
        return done.wait(timeout_s)

    # ------------------------------------------------------------------ internals
    def _submit(self, op) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._loop, name="grafana-annotator", daemon=True)
            self._worker.start()
        self._q.put(op)

    def _loop(self) -> None:
        with httpx.Client(base_url=self.base_url, auth=self.auth, timeout=self.timeout_s) as client:
            while True:
                kind, a, b = self._q.get()
                try:
                    if kind == "flush":
                        a.set()
                    elif self.failures < MAX_CONSECUTIVE_FAILURES:
                        self._do(client, kind, a, b)
                except Exception as e:  # noqa: BLE001 - a marker must never matter to a run
                    self._fail(f"{type(e).__name__}: {e}")
                finally:
                    if kind == "post":
                        a.done.set()

    def _do(self, client: httpx.Client, kind: str, m: Marker, end: float | None) -> None:
        body = {"time": _ms(m.start), "text": m.text, "tags": m.tags}
        if kind == "post":
            r = client.post("/api/annotations", json=body)
            r.raise_for_status()
            m.id = int(r.json()["id"])
        elif m.id is not None:     # PUT only when the POST succeeded
            r = client.put(f"/api/annotations/{m.id}", json={**body, "timeEnd": _ms(end if end is not None else time.time())})
            r.raise_for_status()
        self.failures = 0

    def _fail(self, why: str) -> None:
        self.failures += 1
        self.last_error = why
        if self.failures >= MAX_CONSECUTIVE_FAILURES and not self._warned:
            self._warned = True
            self.say(f"[annotate] Grafana markers are off for this run ({why}). The run itself is not affected. "
                     "Check `make dashboards`, or set LAB_ANNOTATE=0 to silence this.")


def post_now(text: str, tags: list[str], *, base_url: str | None = None, auth: tuple[str, str] | None = None, timeout_s: float = 5.0) -> int:
    """Synchronous single marker for `slo-lab annotate`. Unlike the run hooks this RAISES, because the user asked for it."""
    with httpx.Client(base_url=(base_url or cfg.GRAFANA_URL).rstrip("/"), auth=auth or (cfg.GRAFANA_USER, cfg.GRAFANA_PASSWORD), timeout=timeout_s) as c:
        r = c.post("/api/annotations", json={"time": _ms(time.time()), "text": text, "tags": [TAG, *tags]})
        r.raise_for_status()
        return int(r.json()["id"])
