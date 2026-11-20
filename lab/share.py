"""`make share`: pack everything an analysis needs into ONE zip, so results can be attached to a chat or e-mail
instead of being photographed from a terminal.

Included: results/ (run directories, calibration, capacity, REPORT.md, CSVs, diagnostics), config/*.yaml (your measured
load/SLO, the matrix, detectors, autoscaling overlay) and platform/versions.lock. Never included: models, the virtualenv,
anything outside the project, and single files above `max_file_mb` (listed in SHARE-INFO.txt instead).
Nothing here reads credentials: the lab never stores any (kubectl/helm use your own kubeconfig, which is not in the project).
"""
from __future__ import annotations

import json
import os
import platform
import time
import zipfile
from pathlib import Path

WINDOWS_SYSTEM_USERS = {"Public", "Default", "Default User", "All Users", "desktop.ini"}


def _files(base: Path) -> list[Path]:
    if not base.exists():
        return []
    skip_dirs = {"__pycache__", ".git", ".venv"}
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and not (set(p.relative_to(base).parts) & skip_dirs):
            out.append(p)
    return out


def collect(root: Path, results_dir: Path, config_dir: Path) -> list[Path]:
    """Files that go into the zip (absolute paths, all below `root`)."""
    files = _files(results_dir)
    files += sorted(p for p in config_dir.glob("*.yaml") if p.is_file()) if config_dir.exists() else []
    files += sorted(p for p in config_dir.glob("*.yaml.example") if p.is_file()) if config_dir.exists() else []
    lock = root / "platform" / "versions.lock"
    if lock.exists():
        files.append(lock)
    # never pack a previous share zip, whatever directory it was written to
    return [p for p in files if not (p.name.startswith("results-share-") and p.suffix == ".zip")]


def run_inventory(runs_dir: Path) -> dict[str, int]:
    """phase -> number of finished runs (a run is finished when it has a DONE marker)."""
    inv: dict[str, int] = {}
    if runs_dir.exists():
        for phase in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            inv[phase.name] = sum(1 for r in phase.iterdir() if (r / "DONE").exists())
    return inv


def build_share(root: Path, results_dir: Path, config_dir: Path, out: Path, max_file_mb: float = 25.0) -> dict:
    """Write the zip. Returns {path, n_files, bytes, skipped:[(name, mb)], inventory}."""
    limit = int(max_file_mb * 1024 * 1024)
    skipped: list[tuple[str, float]] = []
    n = 0
    inventory = run_inventory(results_dir / "runs")
    out = out.resolve()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in collect(root, results_dir, config_dir):
            if p.resolve() == out:
                continue
            size = p.stat().st_size
            if size > limit:
                skipped.append((str(p.relative_to(root)), round(size / 1024 / 1024, 1)))
                continue
            z.write(p, arcname=str(p.relative_to(root)))
            n += 1
        info = {
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "finished_runs_per_phase": inventory,
            "python": platform.python_version(),
            "skipped_too_large": skipped,
            "files": n,
        }
        z.writestr("SHARE-INFO.txt", json.dumps(info, indent=2) + "\n")
    return {"path": out, "n_files": n + 1, "bytes": out.stat().st_size, "skipped": skipped, "inventory": inventory}


def windows_download_candidates(users_dir: Path = Path("/mnt/c/Users")) -> list[Path]:
    """Downloads folders of every real Windows profile (system profiles skipped), sorted."""
    try:
        return sorted(d / "Downloads" for d in users_dir.iterdir() if d.name not in WINDOWS_SYSTEM_USERS and (d / "Downloads").is_dir())
    except OSError:
        return []


def windows_downloads(users_dir: Path = Path("/mnt/c/Users")) -> Path | None:
    """The Windows Downloads folder when it can be determined without guessing (exactly one real user profile has one)."""
    cands = windows_download_candidates(users_dir)
    return cands[0] if len(cands) == 1 else None


def wsl_unc_path(p: Path) -> str | None:
    """Path of `p` as Windows Explorer sees it (\\\\wsl.localhost\\<distro>\\...); None outside WSL."""
    distro = os.getenv("WSL_DISTRO_NAME")
    if not distro:
        return None
    return "\\\\wsl.localhost\\" + distro + str(p.resolve()).replace("/", "\\")
