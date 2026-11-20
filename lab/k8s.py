"""Thin subprocess wrappers around kubectl/helm. Every call has a timeout; failures raise with output."""
from __future__ import annotations

import subprocess
import time

from .config import CHART_DIR, CONFIG_DIR, KEDA_POLICIES, KUBE_CONTEXT, NAMESPACE, RELEASE, ROOT, STATIC_POLICIES


class CmdError(RuntimeError):
    pass


class LabNotReady(RuntimeError):
    """The cluster/chart the harness needs is not deployed. Message says exactly what to run."""


def sh(cmd: list[str], *, check: bool = True, timeout: float = 600.0) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CmdError(f"timeout after {timeout}s: {' '.join(cmd)}") from e
    if check and p.returncode != 0:
        raise CmdError(f"{' '.join(cmd)}\nexit={p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def kubectl(*args: str, check: bool = True, timeout: float = 120.0) -> str:
    return sh(["kubectl", "--context", KUBE_CONTEXT, *args], check=check, timeout=timeout).stdout.strip()


def helm(*args: str, check: bool = True, timeout: float = 600.0) -> str:
    return sh(["helm", "--kube-context", KUBE_CONTEXT, *args], check=check, timeout=timeout).stdout.strip()


class ScenarioNotRunnable(LabNotReady):
    """This scenario cannot produce valid data with the CURRENT deployment/config (not a transient infrastructure problem:
    retrying or healing will not help). The message says what to change."""


def release_status() -> str | None:
    """Helm status of the lab release ('deployed', 'failed', 'pending-upgrade', ...) or None when it does not exist."""
    import json
    try:
        out = helm("status", RELEASE, "-n", NAMESPACE, "-o", "json", timeout=60)
    except CmdError:
        return None
    return (json.loads(out).get("info") or {}).get("status")


def ensure_release_deployed() -> None:
    """Fail early and clearly (instead of a Helm traceback) when `make up` has not finished."""
    try:
        out = helm("status", RELEASE, "-n", NAMESPACE, "-o", "json", timeout=60)
    except CmdError as e:
        raise LabNotReady(
            f"Helm release '{RELEASE}' is not deployed in namespace '{NAMESPACE}' (or the cluster is unreachable).\n"
            "FIRST run `docker ps`: if it prints an error, Docker Desktop is stopped or its WSL integration is off (the cluster is NOT lost): "
            "start Docker Desktop, wait for 'Engine running', then `make heal && make verify`.\n"
            "Only if `docker ps` works and shows no slo-loop-control-plane: run `make up` and wait for 'everything verified'. "
            "If `make up` was interrupted, `make images && make deploy && make verify` resumes it."
        ) from e
    import json
    status = (json.loads(out).get("info") or {}).get("status")
    if status != "deployed":
        raise LabNotReady(f"Helm release '{RELEASE}' is in state '{status}', expected 'deployed'. Run `make deploy`, then `make verify`.")


def gateway_env(name: str) -> str | None:
    """Value of an environment variable of the RUNNING gateway deployment (what the pods actually use), or None."""
    import json
    spec = json.loads(kubectl("-n", NAMESPACE, "get", "deploy", "gateway", "-o", "json"))
    for e in spec["spec"]["template"]["spec"]["containers"][0].get("env") or []:
        if e.get("name") == name:
            return e.get("value")
    return None


GATEWAY_DEFAULT_RETRIEVE_TIMEOUT_S = 10.0  # apps/gateway/app.py default when the chart does not set the variable


def retrieve_timeout_s() -> float:
    v = gateway_env("RETRIEVE_TIMEOUT_S")
    try:
        return float(v) if v else GATEWAY_DEFAULT_RETRIEVE_TIMEOUT_S
    except ValueError:
        return GATEWAY_DEFAULT_RETRIEVE_TIMEOUT_S


def deployment_replicas(name: str = "llm") -> tuple[int, int]:
    """(spec.replicas, ready replicas)"""
    out = kubectl("-n", NAMESPACE, "get", "deploy", name, "-o", "jsonpath={.spec.replicas} {.status.readyReplicas}")
    parts = out.split()
    spec = int(parts[0]) if parts and parts[0] else 0
    ready = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    return spec, ready


def wait_ready(name: str, want: int, timeout_s: float = 360.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        spec, ready = deployment_replicas(name)
        if spec == want and ready == want:
            return
        time.sleep(3)
    raise CmdError(f"deployment/{name} did not reach {want} ready replicas in {timeout_s}s (spec/ready={deployment_replicas(name)})")


def restart_all(timeout_s: float = 360.0) -> None:
    kubectl("-n", NAMESPACE, "rollout", "restart", "deploy/llm", "deploy/gateway", "deploy/retriever")
    for d in ("llm", "gateway", "retriever"):
        kubectl("-n", NAMESPACE, "rollout", "status", f"deploy/{d}", f"--timeout={int(timeout_s)}s", timeout=timeout_s + 30)


def helm_set_policy(policy: str) -> None:
    helm(
        "upgrade", RELEASE, str(CHART_DIR), "-n", NAMESPACE,
        "--reuse-values", "--set", f"autoscaling.policy={policy}",
        "--wait", "--timeout", "6m",
        timeout=420,
    )


AUTOSCALING_FILE = CONFIG_DIR / "autoscaling.yaml"  # optional: your own cpu/queue/burn thresholds (see the .example file)
AUTOSCALING_KEYS = {"cpu", "queue", "burn", "minReplicas", "maxReplicas", "pollingInterval",
                    "scaleUpStabilizationSeconds", "scaleDownStabilizationSeconds"}


def autoscaling_overlay(path=None) -> dict | None:
    """Load and validate config/autoscaling.yaml; None when the file does not exist.

    Only `autoscaling:` keys that shape a trigger are accepted. `policy` is rejected on purpose: the runner chooses the
    policy per run, so a policy in this file would silently be overwritten (or, worse, silently pin one).
    """
    import yaml
    path = path or AUTOSCALING_FILE
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or set(data) - {"autoscaling"}:
        raise ValueError(f"{path.name}: the only top-level key allowed is 'autoscaling:' (found {sorted(set(data) - {'autoscaling'})})")
    asc = data.get("autoscaling") or {}
    bad = sorted(set(asc) - AUTOSCALING_KEYS)
    if bad:
        hint = " (the policy is chosen per run by the experiment matrix)" if "policy" in bad else ""
        raise ValueError(f"{path.name}: unknown autoscaling key(s) {bad}{hint}. Allowed: {sorted(AUTOSCALING_KEYS)}")
    for trig in ("cpu", "queue", "burn"):
        if trig in asc and not isinstance(asc[trig], dict):
            raise ValueError(f"{path.name}: autoscaling.{trig} must be a mapping")
    return asc


def values_file_args() -> list[str]:
    """`-f` arguments for a full deploy, in precedence order: slo.yaml < autoscaling.yaml < images.yaml."""
    args = ["-f", str(CONFIG_DIR / "slo.yaml")]
    if autoscaling_overlay() is not None:  # validates before helm ever sees it
        args += ["-f", str(AUTOSCALING_FILE)]
    images = CONFIG_DIR / "images.yaml"  # written by `make pin-images` (digest pins); optional
    if images.exists():
        args += ["-f", str(images)]
    return args


def effective_autoscaling() -> dict:
    """The autoscaling values the DEPLOYED release actually uses (helm get values --all)."""
    import json
    vals = json.loads(helm("get", "values", RELEASE, "-n", NAMESPACE, "-o", "json", "--all", timeout=60))
    return vals.get("autoscaling", {})


def helm_upgrade_full(extra: list[str] | None = None) -> None:
    """Authoritative deploy: chart defaults + config/slo.yaml (+ config/autoscaling.yaml, config/images.yaml) + any --set."""
    cmd = [
        "upgrade", "--install", RELEASE, str(CHART_DIR), "-n", NAMESPACE, "--create-namespace",
        "--reset-values", *values_file_args(),
    ]
    cmd += ["--wait", "--timeout", "6m"]
    helm(*cmd, *(extra or []), timeout=420)


def autoscalers_present() -> list[str]:
    """Names of KEDA ScaledObjects / HPAs that currently exist in the lab namespace (they own the llm replica count)."""
    out = kubectl("-n", NAMESPACE, "get", "scaledobjects.keda.sh,hpa", "-o", "name", check=False)
    return [ln for ln in out.splitlines() if ln.strip()]


def settle_one_replica(timeout_s: float = 180.0, poll_s: float = 3.0) -> None:
    """Make the llm Deployment exactly 1 replica after `helm upgrade ... policy=static1`.

    Real incident (10 Nov, after a cpu-policy run): Helm deleted the ScaledObject and set replicas=1, but the HPA that KEDA
    owns is garbage-collected ASYNCHRONOUSLY. For a few seconds it still saw the burst's high CPU and scaled the Deployment
    back to 2; once it was gone, nothing scaled it down again, and `wait_ready('llm', 1)` timed out with spec/ready=(2, 2).
    So: wait until no autoscaler is left, then (idempotently) set the replica count ourselves.
    """
    deadline = time.time() + timeout_s
    while autoscalers_present() and time.time() < deadline:
        time.sleep(poll_s)
    spec, _ready = deployment_replicas("llm")
    if spec != 1:
        kubectl("-n", NAMESPACE, "scale", "deploy/llm", "--replicas=1")


BURN_QUIET_QUERY = "max(slo:burn_rate:request_120s) or vector(0)"


def wait_burn_quiet(max_burn: float = 1.0, timeout_s: float = 240.0, poll_s: float = 5.0, say=print) -> float | None:
    """Before the `burn` policy is switched on: wait until the 120 s burn rate has fallen to ~0 (two calm polls in a row).

    Real incident (11 Nov): s2_long_shift__burn started with 3 replicas instead of 1 (run flagged invalid). The burn trigger reads a
    120 s window, and Prometheus keeps answering with the series of the gateway pod that was just replaced for up to 2 minutes, so the
    tail of the PREVIOUS run (s8_ramp ends in overload) made KEDA scale out before this run had sent a single request. Best effort:
    returns None at once when Prometheus is unreachable, and never blocks longer than `timeout_s`.
    """
    from . import prom

    deadline = time.time() + timeout_s
    calm = 0
    while True:
        last = prom.instant(BURN_QUIET_QUERY)
        if last is None:
            return None
        calm = calm + 1 if last <= max_burn else 0
        if calm >= 2:
            return last
        if time.time() >= deadline:
            say(f"[prep] burn rate is still {last:.1f} after {timeout_s:.0f}s; starting anyway (a run that starts with extra replicas is flagged invalid)")
            return last
        time.sleep(poll_s)


def set_policy(policy: str) -> None:
    """Put the llm tier into a known state for `policy` with fresh pods everywhere.

    Sequence: static1 (removes any ScaledObject, 1 replica) -> restart all pods (clean slate)
    -> target policy. KEDA policies therefore always start from exactly 1 replica.
    """
    if policy not in STATIC_POLICIES + KEDA_POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    ensure_release_deployed()
    helm_set_policy("static1")
    settle_one_replica()
    wait_ready("llm", 1)
    restart_all()
    wait_ready("llm", 1)
    if policy == "static1":
        return
    if policy == "burn":
        wait_burn_quiet()
    helm_set_policy(policy)
    if policy.startswith("static"):
        wait_ready("llm", int(policy.removeprefix("static")))


def apply_manifest(path: str) -> None:
    kubectl("apply", "-f", path)


def delete_manifest(path: str) -> None:
    kubectl("delete", "-f", path, "--ignore-not-found", "--wait=false", check=False)


def recover(say=print) -> None:
    """Best-effort repair of the lab between runs (also `make recover`).

    1. scripts/heal.sh: node running, model visible inside the node (restarts the node container when the bind mount
       went stale), pods restarted.
    2. A Helm release left in state 'failed' by an interrupted/timed-out upgrade is upgraded in place (Helm allows that and
       writes a new, deployed revision); 'pending-*' states need a human (`helm rollback`) and are reported as such.
    Raises LabNotReady with the reason when the lab cannot be repaired automatically.
    """
    heal_err = None
    p = sh(["bash", str(ROOT / "scripts" / "heal.sh")], check=False, timeout=1500)
    for line in (p.stdout + p.stderr).strip().splitlines()[-6:]:
        say(f"[recover] heal: {line.strip()}")
    if p.returncode != 0:
        heal_err = f"scripts/heal.sh exited {p.returncode}"
    status = release_status()
    if status is None:
        raise LabNotReady(f"cannot recover: Helm release '{RELEASE}' does not exist or the cluster is unreachable ({heal_err or 'heal ok'}). Run `make up`.")
    if status.startswith("pending"):
        raise LabNotReady(f"Helm release '{RELEASE}' is stuck in '{status}'. Run: helm --kube-context {KUBE_CONTEXT} -n {NAMESPACE} rollback {RELEASE}   then `make verify`.")
    if heal_err or status != "deployed":
        say(f"[recover] helm release state '{status}'" + (f", {heal_err}" if heal_err else "") + ": re-applying the release (static1)")
        try:
            helm_set_policy("static1")
        except CmdError as e:
            raise LabNotReady(f"cannot recover automatically ({e}). Run `make diag` and paste results/diag-*.txt.") from e
    ensure_release_deployed()
    if autoscalers_present():  # interrupted during a cpu/queue/burn run: the autoscaler may be holding the replica count away from 1
        say("[recover] an autoscaler is still active: re-applying static1")
        helm_set_policy("static1")
    settle_one_replica()
    wait_ready("llm", 1)
    say("[recover] lab is ready")
