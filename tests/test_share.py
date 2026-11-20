"""v2.5.1: `make share` (one zip instead of screenshots), the candidate autoscaler thresholds, and a guard that every
document the repo points at exists. Offline; no cluster."""
from __future__ import annotations

import json
import re
import zipfile

import pytest

from conftest import ROOT
from lab import cli, k8s, share
from lab import config as cfg

read = lambda p: (ROOT / p).read_text()  # noqa: E731


def make_project(tmp_path):
    root = tmp_path / "proj"
    runs = root / "results" / "runs" / "A" / "s0_baseline__static1__r1"
    runs.mkdir(parents=True)
    for name, text in {"requests.csv": "t_send\n1\n", "meta.json": "{}", "events.json": "{}", "DONE": "ok\n"}.items():
        (runs / name).write_text(text)
    unfinished = root / "results" / "runs" / "A" / "s1_burst__static1__r1"
    unfinished.mkdir()
    (unfinished / "requests.csv").write_text("t_send\n")           # no DONE marker: not counted as finished
    (root / "results" / "REPORT.md").write_text("# report\n")
    (root / "results" / "huge.bin").write_bytes(b"0" * (3 * 1024 * 1024))
    cfgd = root / "config"
    cfgd.mkdir()
    (cfgd / "load.yaml").write_text("load: {base_rps: 0.178}\n")
    (cfgd / "slo.yaml").write_text("slo: {target: 0.99}\n")
    (cfgd / "autoscaling.yaml.example").write_text("autoscaling: {}\n")
    (root / "platform").mkdir()
    (root / "platform" / "versions.lock").write_text("kps 1.2.3\n")
    (root / "models").mkdir()
    (root / "models" / "model.gguf").write_bytes(b"x" * 1024)     # must never be packed
    return root


def test_share_packs_results_and_config_but_not_models_or_oversized_files(tmp_path):
    root = make_project(tmp_path)
    out = tmp_path / "out.zip"
    info = share.build_share(root, root / "results", root / "config", out, max_file_mb=1)
    names = set(zipfile.ZipFile(out).namelist())
    assert {"results/REPORT.md", "results/runs/A/s0_baseline__static1__r1/requests.csv", "config/load.yaml", "config/slo.yaml",
            "config/autoscaling.yaml.example", "platform/versions.lock", "SHARE-INFO.txt"} <= names
    assert not any(n.startswith("models/") for n in names)
    assert "results/huge.bin" not in names and info["skipped"] == [("results/huge.bin", 3.0)]
    meta = json.loads(zipfile.ZipFile(out).read("SHARE-INFO.txt"))
    assert meta["finished_runs_per_phase"] == {"A": 1} and meta["skipped_too_large"] == [["results/huge.bin", 3.0]]


def test_share_never_packs_an_earlier_share_zip_or_itself(tmp_path):
    root = make_project(tmp_path)
    (root / "results" / "results-share-20260101-0000.zip").write_bytes(b"PK")
    out = root / "results" / "results-share-now.zip"
    share.build_share(root, root / "results", root / "config", out)
    names = zipfile.ZipFile(out).namelist()
    assert not any(n.endswith(".zip") for n in names)


def test_share_with_no_results_still_works_and_says_so(tmp_path, monkeypatch, capsys):
    root = tmp_path / "empty"
    (root / "config").mkdir(parents=True)
    (root / "config" / "load.yaml").write_text("load: {}\n")
    monkeypatch.setattr(cfg, "ROOT", root)
    monkeypatch.setattr(cfg, "RESULTS_DIR", root / "results")
    monkeypatch.setattr(cfg, "RUNS_DIR", root / "results" / "runs")
    monkeypatch.setattr(cfg, "CONFIG_DIR", root / "config")
    monkeypatch.setattr(share, "windows_downloads", lambda *a, **k: None)
    assert cli.main(["share", "--out", str(tmp_path / "e.zip")]) == 0
    cap = capsys.readouterr()
    assert "no finished runs" in cap.err and "finished runs per phase: none" in cap.out and (tmp_path / "e.zip").exists()


def test_share_command_copies_to_windows_downloads_when_unambiguous(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setattr(cfg, "ROOT", root)
    monkeypatch.setattr(cfg, "RESULTS_DIR", root / "results")
    monkeypatch.setattr(cfg, "RUNS_DIR", root / "results" / "runs")
    monkeypatch.setattr(cfg, "CONFIG_DIR", root / "config")
    monkeypatch.setattr(share, "windows_downloads", lambda *a, **k: dl)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert cli.main(["share", "--out", str(tmp_path / "s.zip")]) == 0
    out = capsys.readouterr().out
    assert (dl / "s.zip").exists() and "Windows Downloads folder" in out and "\\\\wsl.localhost\\Ubuntu\\" in out
    assert cli.main(["share", "--out", str(tmp_path / "t.zip"), "--no-copy"]) == 0
    assert not (dl / "t.zip").exists()


def test_windows_downloads_only_when_exactly_one_real_profile(tmp_path):
    users = tmp_path / "Users"
    for name in ("Public", "Default", "All Users"):
        (users / name / "Downloads").mkdir(parents=True)
    assert share.windows_downloads(users) is None                      # only system profiles
    (users / "suhas" / "Downloads").mkdir(parents=True)
    assert share.windows_downloads(users) == users / "suhas" / "Downloads"
    (users / "other" / "Downloads").mkdir(parents=True)
    assert share.windows_downloads(users) is None                      # ambiguous: do not guess
    assert share.windows_downloads(tmp_path / "does-not-exist") is None


def test_wsl_unc_path_is_none_outside_wsl(monkeypatch, tmp_path):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert share.wsl_unc_path(tmp_path) is None
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    assert share.wsl_unc_path(tmp_path).startswith("\\\\wsl.localhost\\Ubuntu-22.04\\")


def test_share_make_target_and_ignore_rules():
    mk = read("Makefile")
    assert re.search(r"^share:", mk, re.M) and "$(LAB) share" in mk and "share" in mk.split("\n")[7]
    gi = read(".gitignore")
    assert "results-share-*.zip" in gi and "config/autoscaling.yaml" in gi


# ------------------------------------------------------------------------------------------- candidate thresholds
def test_candidate_overlay_is_valid_and_uses_recording_rules_that_exist():
    ov = k8s.autoscaling_overlay(ROOT / "config" / "autoscaling.candidate.yaml")
    assert ov["queue"]["threshold"] == 6 and ov["burn"]["threshold"] == 10 and ov["cpu"]["utilization"] == 60
    rule = read("charts/slo-loop-app/templates/prometheusrule.yaml")
    window = re.search(r"slo:burn_rate:request_(\d+s)", ov["burn"]["query"]).group(1)
    assert f'"{window}"' in re.search(r'list ("\d+s" )+"\d+s"', rule).group(0)      # the recording rule for that window is generated
    chart = cfg.load_yaml(cfg.CHART_DIR / "values.yaml")["autoscaling"]
    assert ov["queue"]["query"] == chart["queue"]["query"]                           # only thresholds differ, not what is measured


def test_candidate_overlay_is_accepted_by_the_values_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(read("charts/slo-loop-app/values.schema.json"))
    values = cfg.load_yaml(cfg.CHART_DIR / "values.yaml")
    ov = k8s.autoscaling_overlay(ROOT / "config" / "autoscaling.candidate.yaml")
    for trig, kv in ov.items():
        values["autoscaling"][trig].update(kv)
    jsonschema.validate(values, schema)


# ------------------------------------------------------------------------------------------- documents the repo points at
def test_every_doc_the_repo_points_at_exists():
    """README, DESIGN, the example/candidate overlays, the CLI and the scripts mention docs/*.md files; each must exist."""
    sources = [ROOT / "README.md", ROOT / "docs" / "DESIGN.md", ROOT / "docs" / "TROUBLESHOOTING.md", ROOT / "Makefile",
               ROOT / "config" / "autoscaling.yaml.example", ROOT / "config" / "autoscaling.candidate.yaml",
               ROOT / "lab" / "cli.py", ROOT / "lab" / "k8s.py", ROOT / "lab" / "sim.py"]
    sources += sorted((ROOT / "scripts").glob("*.sh")) + sorted((ROOT / "scenarios").glob("*.yaml"))
    missing = {}
    for src in sources:
        for ref in re.findall(r"docs/([A-Za-z0-9_.-]+\.(?:md|example))", src.read_text()):
            if not (ROOT / "docs" / ref).exists():
                missing.setdefault(ref, []).append(src.name)
    assert not missing, f"referenced but missing: {missing}"


def test_autoscaling_doc_numbers_match_what_the_code_does():
    doc = read("docs/AUTOSCALING.md")
    for needle in ("config/autoscaling.candidate.yaml", "make autoscaling", "s0_baseline", ">= 90 %", "scaleDownStabilizationSeconds"):
        assert needle in doc, needle
    cand = read("config/autoscaling.candidate.yaml")
    assert "docs/AUTOSCALING.md" in cand
    # the commands in the doc use options that exist
    for opt in ("--phase X", "--scenario s0_baseline", "--policy $p", "--rep 2", "--force"):
        assert opt in doc, opt


def test_share_prints_ready_to_paste_copy_lines_when_downloads_is_ambiguous(tmp_path, monkeypatch, capsys):
    """No placeholders: a hint with <your-windows-name> was pasted literally and bash read '<' as a redirect (real incident)."""
    root = make_project(tmp_path)
    for name, val in (("ROOT", root), ("RESULTS_DIR", root / "results"), ("RUNS_DIR", root / "results" / "runs"), ("CONFIG_DIR", root / "config")):
        monkeypatch.setattr(cfg, name, val)
    users = tmp_path / "Users"
    for n in ("Prasanna Kumar", "Suhas", "Administrator"):
        (users / n / "Downloads").mkdir(parents=True)
    (users / "Public" / "Downloads").mkdir(parents=True)                       # system profile: never offered
    real = share.windows_download_candidates
    monkeypatch.setattr(share, "windows_download_candidates", lambda: real(users))
    monkeypatch.setattr(share, "windows_downloads", lambda *a, **k: None)
    monkeypatch.setenv("USER", "suhas")
    assert cli.main(["share", "--out", str(tmp_path / "s.zip")]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("  cp ")]
    assert len(lines) == 3 and "<" not in "".join(lines) and ">" not in "".join(lines)
    assert all(f'"{tmp_path / "s.zip"}"' in ln for ln in lines) and not any("Public" in ln for ln in lines)
    assert [ln for ln in lines if "# same name as your Linux user" in ln][0].count("Suhas/") == 1
    assert cli.main(["share", "--out", str(tmp_path / "t.zip"), "--no-copy"]) == 0
    assert "  cp " not in capsys.readouterr().out


def test_windows_download_candidates_skip_system_profiles_and_missing_folders(tmp_path):
    users = tmp_path / "Users"
    (users / "Public" / "Downloads").mkdir(parents=True)
    (users / "NoDownloads").mkdir(parents=True)
    (users / "A b" / "Downloads").mkdir(parents=True)
    assert share.windows_download_candidates(users) == [users / "A b" / "Downloads"]
    assert share.windows_download_candidates(tmp_path / "missing") == []
