"""`make publish-results`: copy the finished tables/figures into docs/results/ so they can be committed; raw runs stay out."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")

PUBLISHED = {"REPORT.md", "ROBUSTNESS.md", "ROBUSTNESS-digest.txt", "accepted-flags.csv", "robust_paired_h1.csv", "phaseA_detection.csv",
             "phaseB_autoscale.csv", "fig_phaseB_budget.png"}
NOT_PUBLISHED = {"requests.csv", "meta.json", "results-share-20261119-1103.zip", "diag-1.txt"}


def _results(tmp: Path) -> Path:
    res = tmp / "results"
    run = res / "runs" / "A" / "s0_baseline__static1__r1"
    run.mkdir(parents=True)
    for n in PUBLISHED:
        (res / n).write_text(f"content of {n}\n")
    (run / "requests.csv").write_text("raw\n")
    (run / "meta.json").write_text("{}\n")
    (res / "results-share-20261119-1103.zip").write_text("zip\n")
    (res / "diag-1.txt").write_text("diag\n")
    return res


def _make(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["make", "-f", str(ROOT / "Makefile"), "publish-results"], cwd=cwd, capture_output=True, text=True, timeout=60)


@needs_make
def test_publish_results_copies_tables_and_figures_but_never_raw_runs(tmp_path):
    _results(tmp_path)
    r = _make(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = tmp_path / "docs" / "results"
    names = {p.name for p in out.iterdir()}
    assert names == PUBLISHED, names ^ PUBLISHED
    assert not (names & NOT_PUBLISHED)
    assert (out / "REPORT.md").read_text() == "content of REPORT.md\n"
    assert "files in docs/results/" in r.stdout


@needs_make
def test_publish_results_refuses_until_analyze_and_robust_have_run(tmp_path):
    res = _results(tmp_path)
    (res / "ROBUSTNESS.md").unlink()
    r = _make(tmp_path)
    assert r.returncode != 0
    assert "make analyze && make robust" in r.stdout + r.stderr
    assert not (tmp_path / "docs" / "results").exists(), "nothing may be copied when the inputs are incomplete"


@needs_make
def test_publish_results_works_with_only_the_two_required_files(tmp_path):
    res = tmp_path / "results"
    res.mkdir()
    (res / "REPORT.md").write_text("r\n")
    (res / "ROBUSTNESS.md").write_text("b\n")
    r = _make(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert {p.name for p in (tmp_path / "docs" / "results").iterdir()} == {"REPORT.md", "ROBUSTNESS.md"}


def test_publish_target_is_listed_in_phony_readme_and_results_doc():
    mk = (ROOT / "Makefile").read_text().splitlines()
    assert any(ln.startswith("publish-results:") for ln in mk) and "publish-results" in " ".join(mk[:12])
    assert "make publish-results" in (ROOT / "README.md").read_text()
    assert "make publish-results" in (ROOT / "docs" / "RESULTS.md").read_text()


def test_results_doc_states_every_hypothesis_verdict_and_the_caveats_that_must_travel_with_them():
    doc = (ROOT / "docs" / "RESULTS.md").read_text()
    for needle in ("**H1**", "**H2**", "**H3**", "**H4**", "Falsified", "multiplicity", "could not have been", "Artifacts removed", "with and without"):
        assert needle in doc, needle
    readme = (ROOT / "README.md").read_text()
    assert "docs/RESULTS.md" in readme and "No result numbers are included" not in readme
