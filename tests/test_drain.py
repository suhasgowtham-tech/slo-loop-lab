"""v2.5.1: scale-in must not kill in-flight requests; tests that must pass on a CALIBRATED machine; diag/verify guards. Offline."""
from __future__ import annotations

import json
import re

import yaml

from conftest import ROOT

read = lambda p: (ROOT / p).read_text()  # noqa: E731


def test_drain_defaults_are_consistent_and_in_the_schema():
    v = yaml.safe_load(read("charts/slo-loop-app/values.yaml"))["llm"]
    assert v["drainSeconds"] >= 20, "streams last up to ~25 s at the calibrated thresholds; a shorter drain still cuts the slowest ones"
    assert v["terminationGraceSeconds"] >= v["drainSeconds"] + 10, "the grace period must outlast the drain or SIGKILL arrives first"
    schema = json.loads(read("charts/slo-loop-app/values.schema.json"))["properties"]["llm"]["properties"]
    assert "drainSeconds" in schema and "terminationGraceSeconds" in schema


def test_template_uses_the_values_instead_of_the_old_hardcoded_grace_period():
    t = read("charts/slo-loop-app/templates/llm.yaml")
    assert "terminationGracePeriodSeconds: 10" not in t
    assert "terminationGracePeriodSeconds: {{ .Values.llm.terminationGraceSeconds }}" in t
    assert re.search(r"preStop:\s+exec:\s+command: \[\"sleep\", \{\{ \.Values\.llm\.drainSeconds \| quote \}\}\]", t)


def test_verify_checks_the_live_drain_and_the_image_has_sleep():
    v = read("scripts/verify.sh")
    assert "lifecycle.preStop.exec.command[0]" in v and "make autoscaling" in v
    assert re.search(r'exec deploy/llm -c llama -- sleep 0', v)


def test_diag_reports_leftover_chaos_experiments():
    d = read("scripts/diag.sh")
    assert "stresschaos,networkchaos,podchaos" in d and "leftover chaos" in d
    for verb in ("delete", "apply", "patch", "scale", "rollout restart", "edit"):
        assert not re.search(rf"\bk\b[^\n]*\b{verb}\b", d), f"diag.sh must stay read-only ({verb})"


def test_calibrated_config_does_not_break_the_shipped_default_test(tmp_path):
    """The test that failed on the user's machine must accept a calibrated load.yaml (profile+classes intact, calibrated: true)."""
    src = read("tests/test_capacity.py")
    assert 'if not load.get("calibrated"):' in src and 'assert load["calibrated"] is False\n    # lite' not in src
