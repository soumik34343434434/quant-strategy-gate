"""Orchestration: verify config -> load bundle -> run every gate -> verdict.

run_check takes a report and a config directory and nothing else. There is
no parameter, flag or mode that skips or softens a gate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import gates as G
from .config import load_active
from .errors import ConfigError, InputError
from .hashing import manifest_sha256, sha256_file, tree_manifest
from .loader import HEX64, load_core, peek_manifest
from .verdict import Verdict

PACKAGE = Path(__file__).resolve().parent


def checker_sha256() -> str:
    files = tree_manifest(PACKAGE)
    cli = PACKAGE.parent / "gate_check.py"
    if cli.is_file():
        files["gate_check.py"] = sha256_file(cli)
    return manifest_sha256(files)


def _all_blocked_at_integrity(v: Verdict, problem: str) -> Verdict:
    first = G.GateResult(0, G.GATES[0].key, G.GATES[0].name)
    first.fail(problem)
    first.finish()
    v.gates = [first] + [G.skipped(g, "not evaluated: integrity failed") for g in G.GATES[1:]]
    return v


def run_check(report_dir: Path, config_dir: Path) -> Verdict:
    report_dir = Path(report_dir)
    v = Verdict(checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), gates=[],
                checker_sha256=checker_sha256())
    peek = peek_manifest(report_dir)
    sv = peek.get("strategy_version")
    v.strategy_version = sv if isinstance(sv, str) and HEX64.match(sv) else None
    v.strategy_id = peek.get("strategy_id") if isinstance(peek.get("strategy_id"), str) else None

    try:
        v.files = tree_manifest(report_dir)
        v.bundle_sha256 = manifest_sha256(v.files)
    except Exception as e:
        return _all_blocked_at_integrity(v, f"report directory unusable: {e}")

    try:
        cfg = load_active(config_dir)
    except ConfigError as e:
        return _all_blocked_at_integrity(v, str(e))
    v.config_version, v.config_files, v.registry_head = cfg.version, cfg.files, cfg.registry_head
    v.limits = dict(cfg.data["deployment_limits"])

    try:
        core = load_core(report_dir, cfg)
    except InputError as e:
        return _all_blocked_at_integrity(v, str(e))
    except Exception as e:
        return _all_blocked_at_integrity(v, f"internal error while loading: {type(e).__name__}: {e}")

    ctx = G.Context(core, cfg)
    v.gates = [G.run_gate(g, ctx) for g in G.GATES]
    try:
        v.metrics = dict(ctx.headline)
    except Exception:
        v.metrics = {}
    return v


__all__ = ["run_check", "checker_sha256"]
