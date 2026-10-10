#!/usr/bin/env python3
"""Template adapter: a backtester's trade export -> canonical trades.csv.

  python adapters/rename_columns.py export.csv mapping.json --source-tz UTC --out report/trades.csv

mapping.json maps canonical column -> your export's column, for example
  {"trade_id": "Trade #", "entry_time": "Entry", "exit_time": "Exit", "side": "Direction",
   "size": "Qty", "entry_price": "Entry Px", "exit_price": "Exit Px", "gross_pnl": "Gross P&L",
   "fees": "Commission", "slippage": "Slippage", "financing": "Funding"}
and optionally "side_values": {"Buy": "long", "Sell": "short"}.

This adapter only renames columns, maps side labels, and stamps a timezone on
naive timestamps when you name one explicitly. It never fills, defaults or
repairs values: a column you cannot map stays missing and the checker blocks.
Adapters sit outside the trust boundary; the checker re-validates everything.
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

CANONICAL = ["trade_id", "entry_time", "exit_time", "side", "size", "entry_price", "exit_price",
             "gross_pnl", "fees", "slippage", "financing"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("mapping", type=Path)
    ap.add_argument("--source-tz", required=True, help="timezone of naive timestamps in the export, e.g. UTC")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    mapping = json.loads(a.mapping.read_text())
    side_values = mapping.pop("side_values", {})
    src = pd.read_csv(a.export, dtype=str, keep_default_na=False)
    out = pd.DataFrame()
    for col in CANONICAL:
        if col in mapping:
            if mapping[col] not in src.columns:
                print(f"export has no column {mapping[col]!r} (mapped to {col})", file=sys.stderr)
                return 1
            out[col] = src[mapping[col]]
    if "side" in out and side_values:
        out["side"] = out["side"].map(lambda v: side_values.get(v, v))
    for col in ("entry_time", "exit_time"):
        if col in out:
            ts = pd.to_datetime(out[col], format="ISO8601")
            ts = ts.dt.tz_localize(a.source_tz) if ts.dt.tz is None else ts
            out[col] = ts.dt.tz_convert("UTC").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(a.out, index=False)
    missing = [c for c in CANONICAL if c not in out]
    print(f"wrote {a.out}" + (f"; NOT mapped (checker will block): {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
