"""Paths and config loading. Everything configurable lives in config/*.yaml, scenarios/*.yaml."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
SCENARIO_DIR = ROOT / "scenarios"
CHAOS_DIR = ROOT / "chaos"
CHART_DIR = ROOT / "charts" / "slo-loop-app"
RESULTS_DIR = ROOT / "results"
RUNS_DIR = RESULTS_DIR / "runs"

NAMESPACE = "slo-loop"
RELEASE = "slo-loop"
# Every kubectl/helm call is pinned to this context so the lab can never touch another cluster
# (e.g. your EKS/AKS/GKE contexts), whatever `kubectl config current-context` says.
KUBE_CONTEXT = os.getenv("LAB_KUBE_CONTEXT", "kind-slo-loop")
GATEWAY_URL = os.getenv("LAB_GATEWAY_URL", f"http://127.0.0.1:{os.getenv('GATEWAY_PORT', '8080')}")
PROM_URL = os.getenv("LAB_PROM_URL", f"http://127.0.0.1:{os.getenv('PROM_PORT', '9090')}")
GRAFANA_URL = os.getenv("LAB_GRAFANA_URL", f"http://127.0.0.1:{os.getenv('GRAFANA_PORT', '3000')}")
# The lab's Grafana is bound to 127.0.0.1 only; this is the local-only login set in platform/values/kube-prometheus-stack.yaml.
GRAFANA_USER = os.getenv("LAB_GRAFANA_USER", "admin")
GRAFANA_PASSWORD = os.getenv("LAB_GRAFANA_PASSWORD", "slo-loop-lab")

KEDA_POLICIES = ("cpu", "queue", "burn")
STATIC_POLICIES = ("static1", "static2", "static3")
ALL_POLICIES = STATIC_POLICIES + KEDA_POLICIES


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_slo() -> dict:
    return load_yaml(CONFIG_DIR / "slo.yaml")["slo"]


def load_load() -> dict:
    return load_yaml(CONFIG_DIR / "load.yaml")["load"]


def load_detectors() -> list[dict]:
    return load_yaml(CONFIG_DIR / "detectors.yaml")["detectors"]


def load_matrix() -> dict:
    return load_yaml(CONFIG_DIR / "matrix.yaml")["phases"]


def load_scenario(name: str) -> dict:
    path = SCENARIO_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"scenario {name!r} not found in {SCENARIO_DIR}")
    sc = load_yaml(path)
    sc.setdefault("name", name)
    sc.setdefault("incidents", [])
    sc.setdefault("faults", [])
    return sc


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


LOAD_HEADER = (
    "# Load model for the whole lab. Rewritten by `make latency`, `make capacity` and `make profile`; safe to edit by hand.\n"
    "# `profile` picks the request sizes (config/profiles.yaml). `make capacity` then measures how many requests per\n"
    "# second ONE llm replica can serve and sets base_rps = base_fraction * capacity, so every scenario scales to your hardware.\n"
)


def write_load_config(updates: dict, remove: tuple[str, ...] = ()) -> None:
    """Merge `updates` into config/load.yaml and drop the keys in `remove` (the file is regenerated, comments aside)."""
    path = CONFIG_DIR / "load.yaml"
    data = load_yaml(path)
    section = data.setdefault("load", {})
    section.update(updates)
    for k in remove:
        section.pop(k, None)
    path.write_text(LOAD_HEADER + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def load_profiles() -> dict:
    return load_yaml(CONFIG_DIR / "profiles.yaml")["profiles"]


def apply_profile(name: str) -> dict:
    """Switch the request-size profile. Capacity and SLO thresholds depend on it, so both are marked stale."""
    profiles = load_profiles()
    if name not in profiles:
        raise KeyError(f"unknown profile {name!r}; choose one of: {', '.join(profiles)}")
    prof = profiles[name]
    write_load_config(
        {"profile": name, "classes": prof["classes"], "calibrated": False, "capacity_1replica_rps": None,
         "base_rps": float(prof.get("safe_base_rps", 0.1))},
        remove=("capacity_probe", "latency_idle"),
    )
    invalidate_slo_calibration("make profile", "the new workload")
    return prof


def invalidate_slo_calibration(who: str, why: str) -> bool:
    """TTFT thresholds were measured under one workload and one offered load; when either changes they are stale."""
    slo_path = CONFIG_DIR / "slo.yaml"
    slo = load_yaml(slo_path)
    if not slo.get("slo", {}).get("calibrated"):
        return False
    slo["slo"]["calibrated"] = False
    slo["slo"].pop("calibration", None)
    slo_path.write_text(f"# Reset by `{who}`: re-run `make calibrate` for {why}.\n" + yaml.safe_dump(slo, sort_keys=False), encoding="utf-8")
    return True
