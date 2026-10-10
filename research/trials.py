"""Append-only log of every parameter set ever evaluated, per strategy.

n_trials for gate 12 is the number of distinct parameter sets in this log. It
only ever grows: trying more variants makes every later Sharpe harder to pass,
which is the point. Commit the log with the research.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def record_trial(log: Path, strategy: str, params: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"strategy": strategy, "params": params,
                            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, sort_keys=True) + "\n")


def count_trials(log: Path, strategy: str) -> int:
    if not log.exists():
        return 0
    seen = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            if entry["strategy"] == strategy:
                seen.add(json.dumps(entry["params"], sort_keys=True))
    return len(seen)
