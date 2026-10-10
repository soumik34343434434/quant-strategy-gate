#!/usr/bin/env python3
"""Download BTC-USD daily candles from Coinbase's public market-data API.

  python research/fetch_btc.py [start YYYY-MM-DD] [end YYYY-MM-DD]

No account or key. Writes research/data/btc_usd_1d.csv (timestamp = candle open,
00:00 UTC) with open/high/low/close/volume. Only complete days are kept; today's
partial candle is dropped. Gaps are reported, not filled: the gate decides
whether they are acceptable.
"""
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400&start={}&end={}"
OUT = Path(__file__).resolve().parent / "data" / "btc_usd_1d.csv"
PER_REQUEST = 300


def fetch(start: datetime, end: datetime) -> list:
    req = urllib.request.Request(URL.format(start.isoformat(), end.isoformat()),
                                 headers={"User-Agent": "strategy-gate-research/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main(argv) -> int:
    start = datetime.fromisoformat(argv[0] if argv else "2016-01-01").replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromisoformat(argv[1]).replace(tzinfo=timezone.utc) if len(argv) > 1 else today - timedelta(days=1)
    rows = {}
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=PER_REQUEST - 1), end)
        for t, low, high, open_, close, vol in fetch(cur, stop):
            day = datetime.fromtimestamp(t, tz=timezone.utc)
            if start <= day <= end:
                rows[day] = (open_, high, low, close, vol)
        cur = stop + timedelta(days=1)
        time.sleep(0.4)
    days = sorted(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for d in days:
            w.writerow([d.strftime("%Y-%m-%dT%H:%M:%SZ"), *rows[d]])
    gaps = [(a.date(), (b - a).days) for a, b in zip(days, days[1:]) if (b - a).days > 1]
    print(f"wrote {len(days)} daily candles {days[0].date()} .. {days[-1].date()} to {OUT}")
    print(f"gaps longer than 1 day: {gaps if gaps else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
