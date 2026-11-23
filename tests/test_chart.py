"""Static verification of the Helm chart WITHOUT a cluster and without the helm binary.

Rendering uses tests/render/main.go, which runs the chart through Go's real text/template engine
(Helm's engine) with a re-implemented subset of Sprig. It cannot prove the chart installs; it proves
the classes of bugs that actually bit this repo: duplicate resources, values/schema drift, selectors
that match nothing, NodePort/kind-mapping drift, PromQL that does not parse, recorded-rule names the
dashboard/KEDA queries depend on but nobody defines, and integer-only Sprig math used on floats.
`make lint` (real `helm lint` + `helm template`) and `make verify` (server-side dry-run) remain the
authoritative checks on your machine.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "slo-loop-app"
POLICIES = ["static1", "static2", "static3", "cpu", "queue", "burn"]
KEDA = {"cpu", "queue", "burn"}

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain needed for the offline chart renderer")


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_values(policy: str = "static1", extra: dict | None = None) -> dict:
    base = yaml.safe_load((CHART / "values.yaml").read_text())
    slo = yaml.safe_load((ROOT / "config" / "slo.yaml").read_text())  # same layering as `-f config/slo.yaml`
    v = deep_merge(base, slo)
    v["autoscaling"]["policy"] = policy
    return deep_merge(v, extra or {})


@pytest.fixture(scope="session")
def renderer(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("render") / "render"
    p = subprocess.run(["go", "build", "-o", str(out), "main.go"], cwd=ROOT / "tests" / "render", capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return out


def render(renderer: Path, values: dict, tmp_path: Path, release: str = "slo-loop", ns: str = "slo-loop"):
    vf = tmp_path / "values.json"
    vf.write_text(json.dumps(values))
    p = subprocess.run([str(renderer), str(CHART), str(vf), release, ns], capture_output=True, text=True)
    return p


def docs(renderer: Path, values: dict, tmp_path: Path) -> list[dict]:
    p = render(renderer, values, tmp_path)
    assert p.returncode == 0, f"render failed:\n{p.stderr}"
    out = [d for d in yaml.safe_load_all(p.stdout) if d]
    assert out, "chart rendered nothing"
    return out


def by_kind(ds: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in ds if d["kind"] == kind}


# --------------------------------------------------------------------------- values / schema
@pytest.mark.parametrize("policy", POLICIES)
def test_merged_values_satisfy_the_schema(policy):
    import jsonschema
    schema = json.loads((CHART / "values.schema.json").read_text())
    jsonschema.validate(load_values(policy), schema)


def test_schema_rejects_bad_values():
    import jsonschema
    schema = json.loads((CHART / "values.schema.json").read_text())
    for patch in ({"autoscaling": {"policy": "turbo"}}, {"slo": {"target": 1.5}}, {"images": {"llm": {"digest": "nope"}}},
                  {"gateway": {"nodePort": 80}}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(load_values(extra=patch), schema)


def test_pin_images_output_is_valid_for_the_schema():
    import jsonschema
    schema = json.loads((CHART / "values.schema.json").read_text())
    pinned = yaml.safe_load("images:\n  llm:\n    digest: \"sha256:" + "a" * 64 + "\"\n")  # what scripts/pin-images.sh writes
    jsonschema.validate(load_values(extra=pinned), schema)


# --------------------------------------------------------------------------- rendering
@pytest.mark.parametrize("policy", POLICIES)
def test_renders_for_every_policy_without_duplicate_resources(renderer, tmp_path, policy):
    ds = docs(renderer, load_values(policy), tmp_path)
    keys = [(d["kind"], d["metadata"]["name"]) for d in ds]
    assert len(keys) == len(set(keys)), f"duplicate resources: {sorted(k for k in keys if keys.count(k) > 1)}"
    kinds = {k for k, _ in keys}
    assert {"Deployment", "Service", "ServiceMonitor", "PrometheusRule", "ConfigMap"} <= kinds
    assert set(by_kind(ds, "Deployment")) == {"llm", "gateway", "retriever"}
    assert set(by_kind(ds, "Service")) == {"llm", "gateway", "retriever"}
    assert set(by_kind(ds, "ServiceMonitor")) == {"llm", "gateway", "retriever"}
    assert ("ScaledObject" in kinds) == (policy in KEDA)
    for d in ds:
        assert d["metadata"].get("name"), d


def test_unknown_policy_fails_loudly(renderer, tmp_path):
    p = render(renderer, load_values("turbo"), tmp_path)
    assert p.returncode != 0 and "autoscaling.policy must be one of" in p.stderr


@pytest.mark.parametrize("policy,replicas", [("static1", 1), ("static2", 2), ("static3", 3)])
def test_static_policies_pin_replicas_and_have_no_scaledobject(renderer, tmp_path, policy, replicas):
    ds = docs(renderer, load_values(policy), tmp_path)
    assert by_kind(ds, "Deployment")["llm"]["spec"]["replicas"] == replicas
    assert not by_kind(ds, "ScaledObject")


@pytest.mark.parametrize("policy,trigger", [("cpu", "cpu"), ("queue", "prometheus"), ("burn", "prometheus")])
def test_keda_policies_hand_replicas_to_keda(renderer, tmp_path, policy, trigger):
    ds = docs(renderer, load_values(policy), tmp_path)
    assert "replicas" not in by_kind(ds, "Deployment")["llm"]["spec"], "KEDA owns replicas; a fixed value would fight the HPA"
    so = by_kind(ds, "ScaledObject")["llm"]
    assert so["spec"]["scaleTargetRef"]["name"] == "llm"
    assert [t["type"] for t in so["spec"]["triggers"]] == [trigger]
    assert so["spec"]["minReplicaCount"] == 1 and so["spec"]["maxReplicaCount"] == 3
    if trigger == "prometheus":
        md = so["spec"]["triggers"][0]["metadata"]
        assert md["serverAddress"] == "http://kps-prometheus.monitoring.svc:9090"
        assert isinstance(md["threshold"], str)  # KEDA wants strings


def test_selectors_ports_and_probes_line_up(renderer, tmp_path):
    ds = docs(renderer, load_values("static1"), tmp_path)
    deps, svcs, sms = by_kind(ds, "Deployment"), by_kind(ds, "Service"), by_kind(ds, "ServiceMonitor")
    for name, svc in svcs.items():
        pod = deps[name]["spec"]["template"]
        assert svc["spec"]["selector"].items() <= pod["metadata"]["labels"].items()
        assert deps[name]["spec"]["selector"]["matchLabels"].items() <= pod["metadata"]["labels"].items()
        cports = {p["name"] for c in pod["spec"]["containers"] for p in c.get("ports", [])}
        for sp in svc["spec"]["ports"]:
            assert sp["targetPort"] in cports, f"{name}: targetPort {sp['targetPort']} is not a container port"
        sm = sms[name]
        assert sm["spec"]["selector"]["matchLabels"].items() <= svc["metadata"]["labels"].items(), f"ServiceMonitor {name} matches no Service"
        for ep in sm["spec"]["endpoints"]:
            assert ep["port"] in {sp["name"] for sp in svc["spec"]["ports"]}
        for c in pod["spec"]["containers"]:
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                if probe in c:
                    assert c[probe]["httpGet"]["port"] in cports
            res = c["resources"]
            assert "requests" in res and "limits" in res


def test_llm_pod_is_a_clean_experimental_subject(renderer, tmp_path):
    llm = by_kind(docs(renderer, load_values("static1"), tmp_path), "Deployment")["llm"]
    c = llm["spec"]["template"]["spec"]["containers"][0]
    assert "livenessProbe" not in c, "a liveness restart under overload would be an uncontrolled fault"
    assert "startupProbe" in c and "readinessProbe" in c
    args = c["args"]
    assert args[args.index("--model") + 1] == "/models/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf"
    assert "--metrics" in args and args[args.index("--parallel") + 1] == "2"
    assert llm["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0


def test_python_services_run_non_root_with_dropped_capabilities(renderer, tmp_path):
    deps = by_kind(docs(renderer, load_values(), tmp_path), "Deployment")
    for n in ("gateway", "retriever"):
        spec = deps[n]["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True and spec["securityContext"]["runAsUser"] == 10001
        sc = spec["containers"][0]["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]


def test_model_filename_is_consistent_across_chart_script_and_docs():
    script = (ROOT / "scripts" / "03-download-model.sh").read_text()
    deploy = (ROOT / "scripts" / "06-deploy-app.sh").read_text()
    chart_file = load_values()["llm"]["modelFile"]
    assert chart_file in script and chart_file in deploy


def test_context_per_slot_fits_the_longest_prompt_class():
    v, load = load_values(), yaml.safe_load((ROOT / "config" / "load.yaml").read_text())["load"]
    per_slot = v["llm"]["ctxSize"] // v["llm"]["parallel"]
    for name, c in load["classes"].items():
        worst = c["words"] * 1.5 + c["max_tokens"] + 150  # 1.5 tokens/word is pessimistic for English; +150 template/context
        assert worst <= per_slot, f"class {name}: ~{worst:.0f} tokens may exceed the {per_slot}-token slot context"


def test_images_follow_the_app_build_script_and_digest_pin(renderer, tmp_path):
    build = (ROOT / "scripts" / "05-build-images.sh").read_text()
    tag = re.search(r'TAG="([^"]+)"', build).group(1)
    v = load_values()
    assert v["images"]["gateway"]["tag"] == tag and v["images"]["retriever"]["tag"] == tag
    assert v["images"]["gateway"]["repository"] == "slo-loop/gateway" and v["images"]["retriever"]["repository"] == "slo-loop/retriever"
    deps = by_kind(docs(renderer, v, tmp_path), "Deployment")
    assert deps["gateway"]["spec"]["template"]["spec"]["containers"][0]["image"] == f"slo-loop/gateway:{tag}"
    assert deps["llm"]["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/ggml-org/llama.cpp:server"
    digest = "sha256:" + "b" * 64
    pinned = by_kind(docs(renderer, load_values(extra={"images": {"llm": {"digest": digest}}}), tmp_path), "Deployment")["llm"]
    assert pinned["spec"]["template"]["spec"]["containers"][0]["image"] == f"ghcr.io/ggml-org/llama.cpp@{digest}"


def test_monitoring_can_be_disabled_for_clusters_without_the_operator(renderer, tmp_path):
    ds = docs(renderer, load_values(extra={"monitoring": {"enabled": False}}), tmp_path)
    assert not {"ServiceMonitor", "PrometheusRule"} & {d["kind"] for d in ds}
    assert "slo-loop-dashboards" not in by_kind(ds, "ConfigMap")


# --------------------------------------------------------------------------- cluster exposure must agree with kind
def test_nodeports_match_kind_port_mappings_and_platform_values():
    kind = yaml.safe_load((ROOT / "platform" / "kind-config.yaml.tmpl").read_text().replace("__MODELS_DIR__", "/x").replace("__GATEWAY_PORT__", "8080").replace("__PROM_PORT__", "9090").replace("__GRAFANA_PORT__", "3000"))
    mappings = {m["containerPort"]: m for m in kind["nodes"][0]["extraPortMappings"]}
    kps = yaml.safe_load((ROOT / "platform" / "values" / "kube-prometheus-stack.yaml").read_text())
    assert load_values()["gateway"]["nodePort"] in mappings
    assert mappings[load_values()["gateway"]["nodePort"]]["hostPort"] == 8080
    assert kps["prometheus"]["service"]["nodePort"] in mappings and mappings[kps["prometheus"]["service"]["nodePort"]]["hostPort"] == 9090
    assert kps["grafana"]["service"]["nodePort"] in mappings and mappings[kps["grafana"]["service"]["nodePort"]]["hostPort"] == 3000
    for m in mappings.values():
        assert m["listenAddress"] == "127.0.0.1", "lab ports must never be exposed to the LAN"


def test_prometheus_address_matches_the_installed_release_name():
    install = (ROOT / "scripts" / "04-install-platform.sh").read_text()
    kps = yaml.safe_load((ROOT / "platform" / "values" / "kube-prometheus-stack.yaml").read_text())
    assert re.search(r"^install kps prometheus-community/kube-prometheus-stack monitoring", install, re.M)
    assert kps["fullnameOverride"] == "kps"
    assert load_values()["autoscaling"]["prometheusAddress"] == "http://kps-prometheus.monitoring.svc:9090"


# --------------------------------------------------------------------------- PromQL / metric names
def _exported_metric_names() -> set[str]:
    names: set[str] = set()
    for f in (ROOT / "apps" / "gateway" / "app.py", ROOT / "apps" / "retriever" / "app.py"):
        for kind, name in re.findall(r'(Counter|Gauge|Histogram)\(\s*"([a-z_]+)"', f.read_text()):
            if kind == "Counter":
                names |= {name if name.endswith("_total") else name + "_total"}
            elif kind == "Histogram":
                names |= {name + "_bucket", name + "_sum", name + "_count"}
            else:
                names.add(name)
    return names


def _promql_exprs(ds: list[dict]):
    for rule in by_kind(ds, "PrometheusRule").values():
        for g in rule["spec"]["groups"]:
            for r in g["rules"]:
                yield (r.get("record") or r["alert"]), r["expr"], "record" in r
    for f in sorted((CHART / "dashboards").glob("*.json")):
        dash = json.loads(f.read_text())
        for p in dash.get("panels", []):
            for t in p.get("targets", []):
                if t.get("expr"):
                    yield f"panel:{f.stem}:{p.get('title')}", t["expr"], False
        for a in dash.get("annotations", {}).get("list", []):
            if a.get("expr"):
                yield f"annotation:{f.stem}:{a.get('name')}", a["expr"], False


def test_every_promql_expression_parses(renderer, tmp_path):
    import promql_parser
    ds = docs(renderer, load_values(), tmp_path)
    n = 0
    for name, expr, _ in _promql_exprs(ds):
        e = re.sub(r"\$__range_s", "900", expr)                       # Grafana variables, in the order that avoids a prefix clash
        e = re.sub(r"\$__rate_interval|\$__interval|\$__range", "1m", e)
        try:
            promql_parser.parse(e)
        except Exception as ex:  # noqa: BLE001
            pytest.fail(f"PromQL for {name!r} does not parse: {expr}\n{ex}")
        n += 1
    assert n >= 60   # the generated dashboards carry several dozen queries; a drop means a dashboard silently lost its panels


def test_burn_rate_divisor_is_computed_by_promql_not_by_integer_only_sprig(renderer, tmp_path):
    ds = docs(renderer, load_values(extra={"slo": {"target": 0.995}}), tmp_path)
    rules = {r["record"]: r["expr"] for g in by_kind(ds, "PrometheusRule")["slo-loop"]["spec"]["groups"] for r in g["rules"] if "record" in r}
    assert "/ (1 - 0.995)" in rules["slo:burn_rate:request_30s"]
    assert "/ (1 - 0.995)" in rules["slo:burn_rate:token_300s"]
    # Sprig's sub/add/mul/div truncate floats to int64: with real Helm `sub 1.0 0.99` is 1, not 0.01.
    for f in (CHART / "templates").glob("*"):
        assert not re.search(r"\{\{[^}]*\b(sub|add|mul|div|mod|round|floor|ceil)\b", f.read_text()), f"{f.name} uses integer-only Sprig math"


def test_every_referenced_series_is_defined_somewhere(renderer, tmp_path):
    ds = docs(renderer, load_values("burn"), tmp_path)
    recorded = {r for _, r, is_rec in [(n, n, rec) for n, _, rec in _promql_exprs(ds)] if is_rec}
    exported = _exported_metric_names()
    kube = re.compile(r"^(kube_|container_|node_|up$|ALERTS)")  # platform metrics (kube-state-metrics, cAdvisor, Prometheus)
    problems = []
    for name, expr, _ in _promql_exprs(ds):
        for ident in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", re.sub(r'"[^"]*"|\{[^}]*\}|\[[^\]]*\]|\$__[a-z_]+', "", expr)):
            if ident in {"sum", "rate", "by", "without", "or", "and", "unless", "vector", "max", "min", "avg", "clamp_min",
                         "histogram_quantile", "increase", "on", "ignoring", "bool", "time", "clamp_max", "count", "le", "class",
                         "stage", "status", "deployment", "namespace", "pod", "container", "label_replace", "irate", "abs", "ceil", "scalar", "avg_over_time"}:
                continue
            if ident.startswith("slo:"):
                if ident not in recorded:
                    problems.append(f"{name}: recording rule {ident} is not defined")
            elif ident.startswith("slo_") or ident.startswith("retriever_"):
                if ident not in exported:
                    problems.append(f"{name}: metric {ident} is not exported by the apps")
            elif not kube.match(ident) and not ident.isdigit():
                problems.append(f"{name}: unrecognised identifier {ident!r}")
    so = by_kind(ds, "ScaledObject")["llm"]
    for t in so["spec"]["triggers"]:
        q = t["metadata"].get("query", "")
        for ident in re.findall(r"slo:[a-z_0-9:]+|slo_[a-z_]+", q):
            assert ident in recorded or ident in exported, f"KEDA query uses undefined series {ident}"
    assert not problems, "\n".join(problems)


def test_lab_snapshot_queries_use_defined_series(renderer, tmp_path):
    from lab import prom
    ds = docs(renderer, load_values(), tmp_path)
    recorded = {n for n, _, rec in _promql_exprs(ds) if rec}
    for q in prom.SNAPSHOT_QUERIES.values():
        for ident in re.findall(r"slo:[a-z_0-9:]+", q):
            assert ident in recorded, f"lab/prom.py queries {ident} but the chart does not record it"


# --------------------------------------------------------------------------- built-in kinds against the Kubernetes OpenAPI schemas
def test_core_kinds_validate_against_kubernetes_schemas(renderer, tmp_path):
    import kubernetes_validate
    for policy in ("static1", "cpu"):
        for d in docs(renderer, load_values(policy), tmp_path):
            if d["kind"] in ("Deployment", "Service", "ConfigMap"):
                kubernetes_validate.validate(d, "1.33", strict=True)


def test_crd_objects_have_the_required_structure(renderer, tmp_path):
    ds = docs(renderer, load_values("queue"), tmp_path)
    so = by_kind(ds, "ScaledObject")["llm"]
    assert so["apiVersion"] == "keda.sh/v1alpha1"
    beh = so["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
    assert set(beh) == {"scaleUp", "scaleDown"}
    for sm in by_kind(ds, "ServiceMonitor").values():
        assert sm["apiVersion"] == "monitoring.coreos.com/v1" and sm["spec"]["endpoints"]
    for g in by_kind(ds, "PrometheusRule")["slo-loop"]["spec"]["groups"]:
        assert g["name"] and g["rules"]
    dash = by_kind(ds, "ConfigMap")["slo-loop-dashboards"]
    assert dash["metadata"]["labels"]["grafana_dashboard"] == "1"
    titles = {k: json.loads(v)["title"] for k, v in dash["data"].items()}
    assert titles == {"slo-loop-health.json": "SLO-Loop · Service health", "slo-loop-incident.json": "SLO-Loop · Incident view",
                      "slo-loop-autoscaling.json": "SLO-Loop · Autoscaling experiment"}


def test_chaos_manifests_target_pods_that_exist_and_stay_in_the_lab_namespace(renderer, tmp_path):
    llm = by_kind(docs(renderer, load_values(), tmp_path), "Deployment")["llm"]
    labels = llm["spec"]["template"]["metadata"]["labels"]
    for f in sorted((ROOT / "chaos").glob("*.yaml")):
        m = yaml.safe_load(f.read_text())
        assert m["apiVersion"] == "chaos-mesh.org/v1alpha1"
        assert m["metadata"]["namespace"] == "slo-loop"
        sel = m["spec"]["selector"]
        assert sel["namespaces"] == ["slo-loop"], "blast radius must be limited to the lab namespace"
        assert sel["labelSelectors"].items() <= labels.items(), f"{f.name} selects no llm pod"


def test_rendered_gateway_service_is_reachable_through_kinds_port_mapping(renderer, tmp_path):
    """Regression: the gateway Service was ClusterIP while kind forwarded 127.0.0.1:8080 -> node port 30080 (nothing listening)."""
    kind = yaml.safe_load((ROOT / "platform" / "kind-config.yaml.tmpl").read_text().replace("__MODELS_DIR__", "/x").replace("__GATEWAY_PORT__", "8080").replace("__PROM_PORT__", "9090").replace("__GRAFANA_PORT__", "3000"))
    host_to_node = {m["hostPort"]: m["containerPort"] for m in kind["nodes"][0]["extraPortMappings"]}
    gw = by_kind(docs(renderer, load_values(), tmp_path), "Service")["gateway"]
    assert gw["spec"]["type"] == "NodePort"
    assert gw["spec"]["ports"][0]["nodePort"] == host_to_node[8080]
    for other in ("llm", "retriever"):  # internal services stay internal
        assert by_kind(docs(renderer, load_values(), tmp_path), "Service")[other]["spec"].get("type", "ClusterIP") == "ClusterIP"


# --------------------------------------------------------------------------- scale-in drain (v2.5.1)
def test_llm_drains_in_flight_requests_on_scale_in(renderer, tmp_path):
    """Without a preStop hook a scaled-in pod gets SIGTERM at once and its streams are cut: one lost request is ~1 % of a run."""
    pod = by_kind(docs(renderer, load_values("queue"), tmp_path), "Deployment")["llm"]["spec"]["template"]["spec"]
    assert pod["containers"][0]["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "30"]
    assert pod["terminationGracePeriodSeconds"] >= 30 + 10
    off = by_kind(docs(renderer, load_values("queue", {"llm": {"drainSeconds": 0, "terminationGraceSeconds": 10}}), tmp_path),
                  "Deployment")["llm"]["spec"]["template"]["spec"]
    assert off["containers"][0]["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "0"] and off["terminationGracePeriodSeconds"] == 10
