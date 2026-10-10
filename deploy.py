#!/usr/bin/env python3
"""Deploy guard. Normally invoked through deploy.sh.

  deploy.sh <strategy_dir> <report_dir>
  python deploy.py retire <strategy_id> --reason TEXT

Deploy proceeds only if:
  1. the strategy source hashes to the report's strategy_version,
  2. a fresh, in-process check of the report under the active config is DEPLOYABLE
     (stored verdict files are never trusted),
  3. fewer than max_live_strategies are live in deployments.json.
The deploy step itself is a stub: this is read-only research with no keys.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "deployments.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse(msg: str) -> int:
    print(f"REFUSED: {msg}")
    return 1


def _load_ledger() -> list:
    if not LEDGER.exists():
        return []
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        raise ValueError("deployments.json is malformed")
    return data["deployments"]


def _save_ledger(entries: list) -> None:
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".deployments.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"deployments": entries}, f, indent=2)
        f.write("\n")
    os.replace(tmp, LEDGER)


def deploy(strategy_dir: Path, report_dir: Path) -> int:
    from gatecheck.hashing import tree_sha256
    from gatecheck.loader import peek_manifest
    from gatecheck.runner import run_check
    from gatecheck.verdict import write_outputs

    try:
        source_version = tree_sha256(strategy_dir)
    except Exception as e:
        return _refuse(f"cannot hash strategy source: {e}")
    declared = peek_manifest(report_dir).get("strategy_version")
    if declared != source_version:
        return _refuse(f"strategy version mismatch: source hashes to {source_version[:12]}..., report is for "
                       f"{str(declared)[:12]}... Every edit is a new version: re-run the backtest, risk review "
                       f"and paper period, then check again.")

    verdict = run_check(report_dir, HERE / "config")
    write_outputs(verdict, HERE / "verdicts")
    print(verdict.summary_text())
    if verdict.verdict != "DEPLOYABLE":
        return _refuse("fresh check is BLOCKED")
    if verdict.strategy_version != source_version:
        return _refuse("verdict is not for this strategy version")

    try:
        entries = _load_ledger()
    except Exception as e:
        return _refuse(f"cannot read deployments ledger: {e}")
    limits = verdict.limits or {}
    live = [e for e in entries if e.get("status") == "live"]
    if len(live) >= int(limits.get("max_live_strategies", 1)):
        names = ", ".join(f"{e.get('strategy_id')}@{str(e.get('strategy_version'))[:12]}" for e in live)
        return _refuse(f"already live: {names}. Retire it first (python deploy.py retire <strategy_id> --reason ...).")

    entries.append({
        "strategy_id": verdict.strategy_id,
        "strategy_version": source_version,
        "config_version": verdict.config_version,
        "bundle_sha256": verdict.bundle_sha256,
        "checker_sha256": verdict.checker_sha256,
        "deployed_at": _now(),
        "status": "live",
        "limits": {k: v for k, v in limits.items() if k != "note"},
    })
    _save_ledger(entries)
    print("\nCLEARED for a limited start. Limits (from config "
          f"{verdict.config_version}):")
    print(f"  - live strategies: at most {limits.get('max_live_strategies')}, assets: at most {limits.get('max_assets')}")
    print(f"  - initial allocation: at most {limits.get('max_initial_allocation_fraction', 0):.0%} of intended capital")
    print(f"  - kill switch: close everything at {limits.get('kill_switch_drawdown', 0):.0%} drawdown")
    print("  DEPLOYABLE is not permission to scale.")
    print("Deploy step: stub. This is a read-only research build with no exchange keys; recorded in deployments.json.")
    return 0


def retire(strategy_id: str, reason: str) -> int:
    if not reason.strip():
        return _refuse("a reason is required")
    entries = _load_ledger()
    hit = [e for e in entries if e.get("strategy_id") == strategy_id and e.get("status") == "live"]
    if not hit:
        return _refuse(f"no live deployment of {strategy_id!r}")
    for e in hit:
        e.update(status="retired", retired_at=_now(), retire_reason=reason.strip())
    _save_ledger(entries)
    print(f"retired {strategy_id}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "deploy" and not any(a.startswith("-") for a in argv[1:]):
        return deploy(Path(argv[1]), Path(argv[2]))
    if len(argv) == 4 and argv[0] == "retire" and argv[2] == "--reason":
        return retire(argv[1], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    if not sys.flags.isolated:
        os.execv(sys.executable, [sys.executable, "-I", str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(HERE))
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print(f"REFUSED: deploy error: {type(e).__name__}: {e}")
        sys.exit(1)
