"""`slo-lab dashboards`: where are the Grafana dashboards, are they loaded, and does Prometheus have what they draw?

Read-only, about 2 seconds. It answers the question you have right after `make deploy`: "is the new chart really in the
cluster?" - the three dashboards come from the Helm chart, so a cluster deployed before v2.8.0 does not have them.
"""
from __future__ import annotations

import webbrowser

import httpx

from . import config as cfg
from . import dashgen

# Recording rules the dashboards depend on (chart: templates/prometheusrule.yaml). If one is missing the chart is older
# than this repo and panels draw nothing or draw a threshold line in the wrong place.
REQUIRED_RULES = ("slo:target_ratio", "slo:ttft_threshold_seconds", "slo:llm_slots", "slo:scaling_threshold",
                  "slo:autoscaling_policy_info", "slo:burn_rate:request_30s", "slo:good_ratio:class_120s")


def catalog() -> list[dict]:
    """uid, title, one-line purpose of each dashboard (read from the generator, so it cannot go stale)."""
    out = []
    for builder in dashgen.DASHBOARDS.values():
        d = builder()
        out.append({"uid": d["uid"], "title": d["title"], "purpose": d["description"].split(" Source:")[0]})
    return out


def url(uid: str, base: str | None = None, *, range_from: str = "now-15m", refresh: str = "5s") -> str:
    return f"{(base or cfg.GRAFANA_URL).rstrip('/')}/d/{uid}?from={range_from}&to=now&refresh={refresh}"


def gather(grafana: str | None = None, prom: str | None = None, timeout_s: float = 4.0) -> dict:
    grafana, prom = (grafana or cfg.GRAFANA_URL).rstrip("/"), (prom or cfg.PROM_URL).rstrip("/")
    res: dict = {"grafana": grafana, "reachable": False, "version": None, "dashboards": {}, "prometheus": None, "rules": {}}
    auth = (cfg.GRAFANA_USER, cfg.GRAFANA_PASSWORD)
    try:
        r = httpx.get(f"{grafana}/api/health", timeout=timeout_s)
        r.raise_for_status()
        res["reachable"], res["version"] = True, r.json().get("version")
    except Exception:  # noqa: BLE001 - reported, not raised
        return res
    for d in catalog():
        try:
            r = httpx.get(f"{grafana}/api/dashboards/uid/{d['uid']}", auth=auth, timeout=timeout_s)
            res["dashboards"][d["uid"]] = "ok" if r.status_code == 200 else ("login" if r.status_code in (401, 403) else "missing")
        except Exception:  # noqa: BLE001
            res["dashboards"][d["uid"]] = "error"
    try:
        for name in REQUIRED_RULES:
            r = httpx.get(f"{prom}/api/v1/query", params={"query": f'count({{__name__="{name}"}})'}, timeout=timeout_s)
            r.raise_for_status()
            res["rules"][name] = bool(r.json().get("data", {}).get("result"))
        res["prometheus"] = True
    except Exception:  # noqa: BLE001
        res["prometheus"] = False
    return res


def decide(s: dict) -> tuple[bool, str]:
    """(everything fine?, the ONE next step)"""
    if not s["reachable"]:
        return False, f"Grafana does not answer at {s['grafana']}. Is the lab up? Run: make status"
    if any(v == "login" for v in s["dashboards"].values()):
        return False, "Grafana rejected the login. The lab password is in platform/values/kube-prometheus-stack.yaml; set LAB_GRAFANA_USER / LAB_GRAFANA_PASSWORD if you changed it."
    missing_dash = [u for u, v in s["dashboards"].items() if v != "ok"]
    missing_rules = [r for r, ok in s["rules"].items() if not ok]
    if missing_dash or missing_rules or s["prometheus"] is False:
        what = []
        if missing_dash:
            what.append(f"{len(missing_dash)} of {len(s['dashboards'])} dashboards are not loaded")
        if missing_rules:
            what.append(f"{len(missing_rules)} recording rule(s) are missing in Prometheus")
        if s["prometheus"] is False:
            what.append("Prometheus does not answer")
        return False, ("; ".join(what) + ". The dashboards ship with the Helm chart, so the cluster still has an older chart. "
                       "Run: make deploy && make verify   (about 3 minutes; it resets the autoscaling policy to static1). "
                       "If you just deployed, wait 30 seconds: Grafana's sidecar loads dashboards on a short delay.")
    return True, "All three dashboards are loaded. Open Service health first. For a recorded incident: make demo"


def report(s: dict) -> str:
    L = [f"Grafana  {s['grafana']}" + (f"   (version {s['version']}, login {cfg.GRAFANA_USER} / {cfg.GRAFANA_PASSWORD}, local only)" if s["reachable"] else "   NOT REACHABLE")]
    for d in catalog():
        st = s["dashboards"].get(d["uid"], "unknown") if s["reachable"] else "unknown"
        mark = {"ok": "OK     ", "missing": "MISSING", "login": "LOGIN? ", "error": "ERROR  "}.get(st, "?      ")
        L.append(f"  {mark} {d['title']:36s} {url(d['uid'], s['grafana'])}")
        L.append(f"          {d['purpose']}")
    if s["reachable"] and s["prometheus"] is not None:
        have = sum(1 for v in s["rules"].values() if v)
        L.append(f"Recording rules the dashboards draw: {have}/{len(REQUIRED_RULES)} present in Prometheus" if s["prometheus"] else "Prometheus: not reachable")
    return "\n".join(L)


def main(open_browser: bool = False) -> int:
    s = gather()
    good, nxt = decide(s)
    print(report(s))
    print(("\nNext: " if good else "\nNEXT: ") + nxt)
    if open_browser and s["reachable"]:
        webbrowser.open(url(catalog()[0]["uid"], s["grafana"]))
    return 0 if good else 1
