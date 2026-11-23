"""The three Grafana dashboards: generated, in sync with the chart, internally consistent, and wired to the CLI."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fake_grafana import FakeGrafana

from lab import cli, dashboards, dashgen, runner
from lab import config as cfg

ROOT = Path(__file__).resolve().parents[1]
BOARDS = {name: json.loads((dashgen.OUT_DIR / name).read_text()) for name in dashgen.DASHBOARDS}
PANELS = [(name, p) for name, d in BOARDS.items() for p in d["panels"] if p["type"] != "row"]
QUERIED = [(name, p) for name, p in PANELS if p.get("targets")]


# --------------------------------------------------------------------------- generator <-> committed JSON
def test_committed_dashboards_equal_what_the_generator_produces():
    assert dashgen.main(["--check"]) == 0, "run `make dashboards-build` and commit the result"


def test_check_mode_detects_a_stale_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dashgen, "OUT_DIR", tmp_path)
    assert dashgen.main(["--check"]) == 1 and "out of date" in capsys.readouterr().err
    assert dashgen.main([]) == 0 and dashgen.main(["--check"]) == 0
    (tmp_path / "slo-loop-health.json").write_text("{}")
    assert dashgen.main(["--check"]) == 1


def test_generation_is_deterministic():
    assert [dashgen.render(b) for b in dashgen.DASHBOARDS.values()] == [dashgen.render(b) for b in dashgen.DASHBOARDS.values()]


def test_the_chart_ships_exactly_the_generated_files():
    on_disk = {p.name for p in (ROOT / "charts" / "slo-loop-app" / "dashboards").glob("*")}
    assert on_disk == set(dashgen.DASHBOARDS), f"stray dashboard file in the chart: {on_disk ^ set(dashgen.DASHBOARDS)}"
    tpl = (ROOT / "charts" / "slo-loop-app" / "templates" / "dashboard.yaml").read_text()
    for name in dashgen.DASHBOARDS:
        assert f'dashboards/{name}' in tpl, f"{name} is generated but the ConfigMap template does not ship it"


# --------------------------------------------------------------------------- structure
def test_uids_titles_and_navigation():
    uids = [d["uid"] for d in BOARDS.values()]
    assert len(set(uids)) == 3 and all(re.fullmatch(r"slo-loop-[a-z]+", u) for u in uids)
    assert all(d["title"].startswith("SLO-Loop · ") for d in BOARDS.values())
    for d in BOARDS.values():
        assert "slo-loop" in d["tags"]
        (link,) = d["links"]
        assert link["type"] == "dashboards" and link["tags"] == ["slo-loop"] and link["keepTime"] and link["includeVars"], \
            "every dashboard must link to the others and keep the time range"
        assert d["graphTooltip"] == 1, "a shared crosshair is what lets a reader line a fault up across panels"
        assert d["schemaVersion"] == 39 and d["templating"]["list"][0]["name"] == "datasource"


def test_panel_ids_are_unique_and_the_grid_has_no_overlap_and_no_gap_at_the_edge():
    for name, d in BOARDS.items():
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids)), name
        taken: dict[tuple[int, int], str] = {}
        for p in d["panels"]:
            g = p["gridPos"]
            assert 0 <= g["x"] and g["x"] + g["w"] <= 24, f"{name}: {p['title']} leaves the 24-column grid"
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    assert (x, y) not in taken, f"{name}: {p['title']} overlaps {taken[(x, y)]}"
                    taken[(x, y)] = p["title"]


def test_every_content_panel_explains_itself():
    """A VP or founder reads these cold. A panel without a description is a panel that needs the author in the room."""
    bare = [f"{n}: {p['title']}" for n, p in PANELS if p["type"] != "text" and not p.get("description", "").strip()]
    assert not bare, bare


def test_every_query_goes_through_the_datasource_variable():
    for name, p in QUERIED:
        assert p["datasource"] == dashgen.DS, (name, p["title"])
        assert all(t["datasource"] == dashgen.DS and t["expr"].strip() and t["refId"] for t in p["targets"]), (name, p["title"])
        refs = [t["refId"] for t in p["targets"]]
        assert len(refs) == len(set(refs)), f"{name}: {p['title']} reuses a refId"


def test_stat_and_gauge_queries_are_instant_where_they_use_the_dashboard_time_range():
    """`increase(x[$__range])` in a range query would be evaluated at every step; only an instant query gives the single number a tile shows."""
    for name, p in QUERIED:
        for t in p["targets"]:
            if "$__range" in t["expr"]:
                assert t.get("instant") is True and p["type"] in ("stat", "bargauge"), f"{name}: {p['title']}"


# --------------------------------------------------------------------------- the dashboard must agree with the alerts, the chart and the config
def test_thresholds_are_not_retyped_in_queries():
    """SLO target, TTFT thresholds and KEDA thresholds come from recording rules. A literal copy would silently disagree after `make calibrate`."""
    slo = cfg.load_slo()
    forbidden = [str(slo["target"])] + [str(v) for v in slo["ttft_s"].values()]
    for name, p in QUERIED:
        for t in p["targets"]:
            for lit in forbidden:
                assert lit not in re.findall(r"\d+\.?\d*", t["expr"]), f"{name}: {p['title']} hard-codes {lit}: {t['expr']}"


def test_burn_bands_equal_the_alert_thresholds():
    rules = (ROOT / "charts" / "slo-loop-app" / "templates" / "prometheusrule.yaml").read_text()
    alert_levels = sorted({float(x) for x in re.findall(r">= ([0-9.]+)", rules)})       # the 6x page and the 2.5x ticket
    assert alert_levels == [2.5, 6.0]
    bands = [v for _, v in dashgen.BURN_STEPS if v is not None]
    assert set(alert_levels) <= set(bands), "the burn-rate colour bands must contain both alert thresholds"
    p = next(p for n, p in PANELS if p["title"] == "Error-budget burn rate")
    lines = [s["value"] for s in p["fieldConfig"]["defaults"]["thresholds"]["steps"] if s["value"] is not None]
    assert lines == alert_levels and p["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"]["mode"] == "dashed"


def test_namespace_matches_the_lab():
    assert dashgen.NS == cfg.NAMESPACE


def test_a_class_has_the_same_colour_on_every_panel():
    seen: dict[str, set[str]] = {}
    for name, p in PANELS:
        for o in p["fieldConfig"].get("overrides", []) if "fieldConfig" in p else []:
            m = re.fullmatch(r"/\^(?:p95 |SLO )?(short|medium|long)\$/", o["matcher"]["options"]) if o["matcher"]["id"] == "byRegexp" else None
            color = next((pr["value"]["fixedColor"] for pr in o["properties"] if pr["id"] == "color"), None)
            if m and color:
                seen.setdefault(m.group(1), set()).add(color)
    assert seen == {c: {col} for c, col in dashgen.CLASS_COLOR.items()}, seen


def test_status_colours_are_the_reserved_ones():
    allowed = {dashgen.GOOD, dashgen.WARN, dashgen.SERIOUS, dashgen.CRIT}
    for name, p in PANELS:
        if p["type"] in ("stat", "bargauge"):
            for s in p["fieldConfig"]["defaults"]["thresholds"]["steps"]:
                assert s["color"] in allowed | {dashgen.BLUE}, (name, p["title"], s["color"])


def test_every_dashboard_shows_faults_runs_and_alerts():
    for name, d in BOARDS.items():
        a = d["annotations"]["list"]
        tagged = {tuple(x["target"]["tags"]) for x in a if "target" in x}
        assert tagged == {("slo-loop", "fault"), ("slo-loop", "run")}, name
        alerts = [x for x in a if x.get("expr")]
        assert len(alerts) == 1 and "ALERTS" in alerts[0]["expr"] and "SLOBurn" in alerts[0]["expr"], name
        assert all(x["enable"] for x in a)


def test_health_page_leads_with_the_answers_a_vp_asks_first():
    d = BOARDS["slo-loop-health.json"]
    tiles = [p["title"] for p in d["panels"] if p["type"] == "stat" and p["gridPos"]["y"] == 0]
    assert tiles[:3] == ["Error budget left", "SLO compliance", "Burn rate (2 min)"]
    assert any(p["type"] == "text" for p in d["panels"]), "a reading guide for people who do not live in SLO language"


def test_verify_script_demands_every_dashboard_and_its_search_matches_every_title():
    sh = (ROOT / "scripts" / "verify.sh").read_text()
    need = int(re.search(r"jq -e 'length >= (\d+)'; \}", sh.split("dash() {")[1].split("\n")[0] + "; }").group(1))
    assert need == len(dashgen.DASHBOARDS), "verify.sh must require ALL dashboards, or a half-provisioned Grafana passes `make verify`"
    query = re.search(r"api/search\?query=([A-Za-z-]+)", sh).group(1)
    assert all(query in d["title"] for d in BOARDS.values()), f"verify.sh searches for {query!r}, which must be in every dashboard title"


# --------------------------------------------------------------------------- `slo-lab dashboards`
UIDS = [d["uid"] for d in dashboards.catalog()]


@pytest.fixture
def lab_servers(monkeypatch):
    made: list[FakeGrafana] = []

    def make(**kw):
        g = FakeGrafana(**kw)
        made.append(g)
        monkeypatch.setattr(cfg, "GRAFANA_URL", g.url)
        monkeypatch.setattr(cfg, "PROM_URL", g.url)
        return g

    yield make
    for g in made:
        g.close()


def test_catalog_matches_the_generator():
    assert UIDS == [d["uid"] for d in BOARDS.values()] and all(d["purpose"] and "Source:" not in d["purpose"] for d in dashboards.catalog())


def test_dashboards_ok(lab_servers, capsys):
    lab_servers(dashboards=set(UIDS), rules=set(dashboards.REQUIRED_RULES))
    assert dashboards.main() == 0
    out = capsys.readouterr().out
    assert out.count("OK     ") == 3 and "7/7 present" in out and "make demo" in out and "/d/slo-loop-health" in out


def test_older_chart_in_the_cluster_says_make_deploy(lab_servers, capsys):
    lab_servers(dashboards=set(), rules={"slo:burn_rate:request_30s", "slo:good_ratio:class_120s"})     # the v2.7 cluster: old rules, no dashboards
    assert dashboards.main() == 1
    out = capsys.readouterr().out
    assert out.count("MISSING") == 3 and "5 recording rule(s) are missing" in out and "make deploy && make verify" in out


def test_one_missing_dashboard_is_named(lab_servers, capsys):
    lab_servers(dashboards=set(UIDS[:2]), rules=set(dashboards.REQUIRED_RULES))
    assert dashboards.main() == 1
    assert "1 of 3 dashboards are not loaded" in capsys.readouterr().out


def test_wrong_login_is_not_reported_as_missing_dashboards(lab_servers, capsys):
    lab_servers(dashboards=set(UIDS), rules=set(dashboards.REQUIRED_RULES), login_ok=False)
    assert dashboards.main() == 1
    out = capsys.readouterr().out
    assert "rejected the login" in out and "make deploy" not in out


def test_grafana_down_says_check_the_lab(monkeypatch, capsys):
    monkeypatch.setattr(cfg, "GRAFANA_URL", "http://127.0.0.1:9")
    assert dashboards.main() == 1
    out = capsys.readouterr().out
    assert "NOT REACHABLE" in out and "make status" in out


def test_cli_registers_the_new_commands(lab_servers, capsys):
    lab_servers(dashboards=set(UIDS), rules=set(dashboards.REQUIRED_RULES))
    assert cli.main(["dashboards"]) == 0


# --------------------------------------------------------------------------- the demo
def test_demo_scenario_is_valid_isolated_and_runnable():
    sc = cfg.load_scenario(cli.DEMO_SCENARIO)
    (f,) = sc["faults"]
    assert 0 < f["at"] < f["until"] < sc["duration_s"], "the fault must start after a quiet lead-in and end before the run does"
    assert sc["incidents"] == [{"start": f["at"], "end": f["until"], "kind": "retriever_latency", "root_cause": "retriever"}]
    slo = cfg.load_slo()
    delay = f["params"]["latency_x_short_thr"] * slo["ttft_s"]["short"]
    assert delay < 0.9 * 30, "refused by check_scenario_runnable on a default deployment (retrieve timeout 30 s)"
    for phase_scenarios in cfg.load_matrix().values():
        assert cli.DEMO_SCENARIO not in phase_scenarios["scenarios"], "the demo must never enter the experiment matrix"


def test_demo_timeline_is_derived_from_the_scenario():
    sc = cfg.load_scenario(cli.DEMO_SCENARIO)
    f = sc["faults"][0]
    text = cli.demo_timeline(sc)
    assert f"+{int(f['at']) // 60}:{int(f['at']) % 60:02d}" in text and f"+{int(f['until']) // 60}:{int(f['until']) % 60:02d}" in text
    assert f"measuring {sc['duration_s']:.0f}s" in text


def test_demo_refuses_to_start_when_the_dashboards_are_not_there(lab_servers, monkeypatch, capsys):
    lab_servers(dashboards=set(), rules=set())
    monkeypatch.setattr(runner, "run_one", lambda *a, **k: pytest.fail("the demo must not start a run without dashboards"))
    assert cli.main(["demo", "--yes"]) == 1
    assert "make deploy" in capsys.readouterr().out


def test_demo_runs_the_incident_in_phase_x_so_it_never_touches_the_results(lab_servers, monkeypatch, capsys):
    lab_servers(dashboards=set(UIDS), rules=set(dashboards.REQUIRED_RULES))
    calls = []
    monkeypatch.setattr(runner, "run_one", lambda *a, **k: calls.append((a, k)))
    assert cli.main(["demo", "--yes", "--policy", "queue"]) == 0
    assert calls == [(("X", "demo_incident", "queue", 1), {"force": True})]
    out = capsys.readouterr().out
    assert "/d/slo-loop-incident" in out and "Win+Alt+R" in out and "FAULT" in out


def test_make_targets_exist_and_are_documented():
    mk = (ROOT / "Makefile").read_text()
    phony = mk.split(".PHONY:")[1].split("\n\n")[0].replace("\\\n", " ")
    for t in ("dashboards", "dashboards-build", "demo"):
        assert re.search(rf"^{t}:.*##", mk, re.M), f"make {t} missing or has no help text"
        assert t in phony.split(), f"{t} not in .PHONY"
    assert (ROOT / "docs" / "DASHBOARDS.md").exists()
