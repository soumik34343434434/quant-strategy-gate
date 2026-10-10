from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
PROJECT_FILES = ["gate_check.py", "deploy.py", "deploy.sh", "strategy_hash.py", "new_config_version.py", "explain.py"]


def failed(v) -> set[int]:
    return {g.id for g in v.gates if g.status == "FAIL"}


def status(v, gate_id: int) -> str:
    return next(g.status for g in v.gates if g.id == gate_id)


def reason(v, gate_id: int) -> str:
    return next(g.reason for g in v.gates if g.id == gate_id)


def detail(v) -> str:
    return "\n" + "\n".join(f"{g.id:>2} {g.key:<13} {g.status:<8} {g.reason}" for g in v.gates)


def copy_project(dest: Path, config_dir: Path) -> Path:
    dest.mkdir(parents=True)
    for name in PROJECT_FILES:
        shutil.copy2(ROOT / name, dest / name)
    shutil.copytree(ROOT / "gatecheck", dest / "gatecheck", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(config_dir, dest / "config")
    if (ROOT / ".venv").is_dir():
        (dest / ".venv").symlink_to(ROOT / ".venv")  # deploy.sh prefers the project venv
    return dest


def clean_env(**extra) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    env.update(extra)
    return env


def run(cmd: list, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          env=env or clean_env(), cwd=cwd, timeout=300)
