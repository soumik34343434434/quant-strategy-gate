#!/usr/bin/env python3
"""The only way to change a threshold, add a benchmark, or register a reviewer key.

  python new_config_version.py --set gates.quality.min_sharpe_exclusive=1.2 --reason "why"
  python new_config_version.py --set 'benchmarks.SOL-USD={"periods_per_year":365,"max_gap_days":3}' --reason "why"
  python new_config_version.py --allowed-signers path/to/allowed_signers --reason "register risk officer"

Values are JSON. Writes config/v<N+1>/ and appends a hash-chained registry entry
with the date, every old -> new value, and the reason. Old versions are never edited.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gatecheck import config as C  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=JSON")
    ap.add_argument("--allowed-signers", type=Path)
    ap.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    sets = {}
    for item in args.set:
        key, sep, raw = item.partition("=")
        if not sep:
            ap.error(f"--set needs KEY=VALUE, got {item!r}")
        try:
            sets[key] = json.loads(raw)
        except json.JSONDecodeError:
            ap.error(f"value for {key} must be JSON (quote strings): {raw!r}")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        version = C.create_version(HERE / "config", sets, reason=args.reason, date=date,
                                   allowed_signers=args.allowed_signers)
    except Exception as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    entry = json.loads((HERE / "config" / "registry.json").read_text())["entries"][-1]
    print(f"created config {version} ({date}): {args.reason}")
    for c in entry["changes"]:
        print(f"  {c['key']}: {c['old']!r} -> {c['new']!r}")
    print("Every strategy must be re-checked under the new version before deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
