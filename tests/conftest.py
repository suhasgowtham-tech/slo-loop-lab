from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LAB_QUIET_WAIT_S", "0")  # tests must never sit waiting for the host's load average


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url: str, timeout: float = 25.0) -> None:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.15)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


class Service:
    def __init__(self, name: str, app_dir: Path, target: str, port: int, env: dict[str, str], log_dir: Path):
        self.name, self.port = name, port
        self.url = f"http://127.0.0.1:{port}"
        self._log = open(log_dir / f"{name}-{port}.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target, "--app-dir", str(app_dir), "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning", "--no-access-log"],
            env={**os.environ, **env}, stdout=self._log, stderr=subprocess.STDOUT,
        )

    def wait(self, path: str) -> "Service":
        try:
            wait_http(self.url + path)
        except RuntimeError:
            self._log.flush()
            raise RuntimeError(f"{self.name} failed to start:\n{Path(self._log.name).read_text()}")
        return self

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()


class Stack:
    """fake llama.cpp + real retriever + real gateway, all as separate processes."""

    def __init__(self, tmp: Path, llm_env: dict | None = None, gw_env: dict | None = None):
        self.services: list[Service] = []
        llm_env = llm_env or {}
        self.llm = self._add("llm", ROOT / "tests", "fake_llama:app", llm_env, "/health")
        self.retriever = self._add("retriever", ROOT / "apps" / "retriever", "app:app", {}, "/healthz")
        self.gateway = self.spawn_gateway(gw_env or {})
        self.tmp = tmp

    def _add(self, name, app_dir, target, env, health) -> Service:
        port = free_port()
        svc = Service(name, app_dir, target, port, env, Path(os.environ.get("TMPDIR", "/tmp")))
        self.services.append(svc)
        return svc.wait(health)

    def spawn_gateway(self, env: dict | None = None, llm_url: str | None = None) -> Service:
        base = {
            "LLM_URL": llm_url or self.llm.url,
            "RETRIEVER_URL": self.retriever.url,
            "SLO_TTFT_SHORT_S": "1.0",
            "SLO_TTFT_MEDIUM_S": "1.5",
            "SLO_TTFT_LONG_S": "2.0",
            "REQUEST_TIMEOUT_S": "20",
        }
        return self._add("gateway", ROOT / "apps" / "gateway", "app:app", {**base, **(env or {})}, "/healthz")

    def stop(self) -> None:
        for s in reversed(self.services):
            s.stop()


@pytest.fixture(scope="session")
def apps_modules():
    """Import the gateway and retriever modules once (they register metrics in the global registry)."""
    out = {}
    os.environ.setdefault("LLM_URL", "http://127.0.0.1:1")
    for name, path in (("gateway_app", ROOT / "apps/gateway/app.py"), ("retriever_app", ROOT / "apps/retriever/app.py")):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("stack"))
    yield s
    s.stop()
