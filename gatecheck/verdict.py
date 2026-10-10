"""Verdict assembly and rendering (SPEC §7). Rendering never changes a status."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .gates import FAIL, NA, PASS, GateResult, fmt

GATE_IDS = list(range(13))
NA_ALLOWED = {6}
NOT_PERMISSION = ("DEPLOYABLE is not permission to scale. Start with one strategy, one asset, small size, "
                  "and a drawdown limit that closes everything.")


def decide(gates: list[GateResult]) -> str:
    """DEPLOYABLE only if every gate 0..12 is present and PASS (or N/A where allowed)."""
    if [g.id for g in gates] != GATE_IDS:
        return "BLOCKED"
    ok = all(g.status == PASS or (g.status == NA and g.id in NA_ALLOWED) for g in gates)
    return "DEPLOYABLE" if ok else "BLOCKED"


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


@dataclass
class Verdict:
    checked_at: str
    gates: list
    strategy_id: str | None = None
    strategy_version: str | None = None
    bundle_sha256: str | None = None
    files: dict = field(default_factory=dict)
    config_version: str | None = None
    config_files: dict = field(default_factory=dict)
    registry_head: str | None = None
    checker_sha256: str | None = None
    metrics: dict = field(default_factory=dict)
    limits: dict | None = None

    @property
    def verdict(self) -> str:
        return decide(self.gates)

    @property
    def exit_code(self) -> int:
        return 0 if self.verdict == "DEPLOYABLE" else 1

    def failed_by_miss(self) -> list[dict]:
        failed = [g for g in self.gates if g.status == FAIL]
        # Non-numeric failures (missing input, KILL, cannot compute) first, then the largest miss.
        failed.sort(key=lambda g: (0, 0.0, g.id) if g.miss() is None else (1, -g.miss(), g.id))
        return [{"id": g.id, "key": g.key, "name": g.name, "status": g.status,
                 "miss": _clean(g.miss()), "reason": g.reason} for g in failed]

    def to_dict(self) -> dict:
        return {
            "tool": "strategy-gate",
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "checked_at": self.checked_at,
            "strategy": {"id": self.strategy_id, "version": self.strategy_version},
            "input": {"bundle_sha256": self.bundle_sha256, "files": self.files},
            "config": {"version": self.config_version, "files": self.config_files,
                       "registry_head": self.registry_head},
            "checker": {"source_sha256": self.checker_sha256},
            "recomputed": {k: _clean(v) for k, v in self.metrics.items()},
            "gates": [{"id": g.id, "key": g.key, "name": g.name, "status": g.status, "reason": g.reason,
                       "checks": [{k: _clean(v) for k, v in c.items()} for c in g.checks]} for g in self.gates],
            "failed_gates_by_miss": self.failed_by_miss(),
            "deployment_limits": self.limits,
            "notice": NOT_PERMISSION,
        }

    def summary_text(self) -> str:
        lines = [f"{self.verdict}  strategy={self.strategy_id} version={(self.strategy_version or '?')[:12]} "
                 f"config={self.config_version}"]
        for g in self.gates:
            lines.append(f"  [{g.status:<7}] {g.id:>2} {g.key:<13} {g.reason[:160]}")
        return "\n".join(lines)


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").replace("`", "'")


def render_md(v: Verdict) -> str:
    d = v.to_dict()
    out = [f"# Strategy gate: **{d['verdict']}**", ""]
    out += ["| | |", "|---|---|",
            f"| Strategy | {_md(v.strategy_id)} `{v.strategy_version}` |",
            f"| Input bundle sha256 | `{v.bundle_sha256}` |",
            f"| Config | {v.config_version} (registry head `{v.registry_head}`) |",
            f"| Checker source sha256 | `{v.checker_sha256}` |",
            f"| Checked at | {v.checked_at} |", ""]
    out += ["## Gates", "", "| # | Gate | Measured | Threshold | Result |", "|---|---|---|---|---|"]
    for g in v.gates:
        if g.checks:
            measured = "; ".join(f"{_md(c['name'])}: {fmt(c['measured'])}" for c in g.checks)
            threshold = "; ".join(f"{c['comparator']} {fmt(c['threshold'])}" for c in g.checks)
        else:
            measured, threshold = "", "—"
        if g.problems:
            measured = "**" + _md("; ".join(g.problems)) + "**" + ("; " + measured if measured else "")
        elif not g.checks:
            measured = _md(g.reason)
        out.append(f"| {g.id} | {_md(g.name)} | {measured} | {threshold} | **{g.status}** |")
    failed = d["failed_gates_by_miss"]
    if failed:
        out += ["", "## Failed gates, furthest miss first", ""]
        for i, f in enumerate(failed, 1):
            miss = "non-numeric (missing input or hard fail)" if f["miss"] is None else f"{f['miss']:.3g}"
            out.append(f"{i}. **{f['key']}** (gate {f['id']}, miss {miss}): {_md(f['reason'])}")
    if v.metrics:
        out += ["", "## Recomputed from raw trades", "", "| Metric | Value |", "|---|---|"]
        out += [f"| {k} | {fmt(val)} |" for k, val in v.metrics.items()]
        out += ["", "Deploy at the same position size relative to equity as max_position_to_equity, or the "
                    "drawdown and return figures above do not apply."]
    if v.limits:
        out += ["", "## Deployment limits", ""]
        out += [f"- {k}: {fmt(val)}" for k, val in v.limits.items() if k != "note"]
    out += ["", f"> {NOT_PERMISSION}", "",
            "_Any plain-English explanation lives in explanation.md, is advisory, and cannot change this verdict._",
            ""]
    return "\n".join(out)


def write_outputs(v: Verdict, verdicts_root: Path) -> Path:
    name = v.strategy_version or f"_invalid/{v.bundle_sha256 or 'unreadable'}"
    out = Path(verdicts_root) / name
    out.mkdir(parents=True, exist_ok=True)
    d = v.to_dict()
    (out / "verdict.json").write_text(json.dumps(d, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "verdict.md").write_text(render_md(v), encoding="utf-8")
    with open(Path(verdicts_root) / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"checked_at": v.checked_at, "verdict": d["verdict"], "strategy": d["strategy"],
                            "bundle_sha256": v.bundle_sha256, "config_version": v.config_version}) + "\n")
    return out
