#!/usr/bin/env python3
"""Backtest the BTC-USD MA trend strategy with vectorbt and write a gate report bundle.

  python research/run_ma_trend.py
  python -I gate_check.py research/output/ma_trend/<strategy_version>

Writes manifest, trades, benchmark, equity (mark-to-market) and sensitivity runs
(base and +/- one step on every parameter). It does not write risk_review.md or
paper/: the review must come from someone else, and paper results from a real
forward period. Until both exist the gate blocks, as it should.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gatecheck.hashing import tree_sha256  # noqa: E402
from research.trials import count_trials, record_trial  # noqa: E402
from research.vbt_export import to_canonical  # noqa: E402

STRATEGY_ID = "btc-ma-trend"
STRATEGY_DIR = ROOT / "research" / "strategies" / "ma_trend"
DATA = ROOT / "research" / "data" / "btc_usd_1d.csv"
TRIALS = ROOT / "research" / "trials.jsonl"
OUTPUT = ROOT / "research" / "output" / "ma_trend"
STEPS = {"fast": 5, "slow": 20}
INIT_CASH = 100_000.0
FEE_BPS, SLIPPAGE_BPS = 10.0, 5.0  # per side; set FEE_BPS to your venue's real taker fee


def load_strategy():
    spec = importlib.util.spec_from_file_location("ma_trend_strategy", STRATEGY_DIR / "strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(index: pd.DatetimeIndex, offset: pd.Timedelta = pd.Timedelta(0)) -> list[str]:
    return [(t + offset).strftime("%Y-%m-%dT%H:%M:%SZ") for t in index]


def backtest(ohlc: pd.DataFrame, params: dict):
    import vectorbt as vbt

    entries, exits = load_strategy().signals(ohlc["close"], params["fast"], params["slow"])
    entries.iloc[-1] = False
    exits.iloc[-1] = True  # close any open position at the last open: the gate takes closed trades only
    pf = vbt.Portfolio.from_signals(close=ohlc["close"], entries=entries, exits=exits, price=ohlc["open"],
                                    fees=FEE_BPS / 1e4, slippage=SLIPPAGE_BPS / 1e4, init_cash=INIT_CASH,
                                    freq="1D")
    trades = to_canonical(pf.trades.records_readable, slippage=SLIPPAGE_BPS / 1e4)
    return pf, trades


def build_bundle(ohlc: pd.DataFrame, out: Path, builder_id: str, n_trials: int, params: dict | None = None) -> Path:
    params = params or json.loads((STRATEGY_DIR / "params.json").read_text())
    if out.exists():
        shutil.rmtree(out)
    (out / "sensitivity").mkdir(parents=True)
    day_end = pd.Timedelta(hours=23, minutes=59, seconds=59)
    idx = ohlc.index

    pf, trades = backtest(ohlc, params)
    trades.to_csv(out / "trades.csv", index=False)
    shutil.copyfile(out / "trades.csv", out / "sensitivity" / "trades_base.csv")
    pd.DataFrame({"timestamp": _iso(idx, day_end), "close": ohlc["close"].to_numpy()}).to_csv(
        out / "benchmark.csv", index=False)
    value = pf.value()
    pd.DataFrame({"timestamp": [_iso(idx[:1])[0]] + _iso(idx, day_end),
                  "equity": np.concatenate([[INIT_CASH], value.to_numpy()])}).to_csv(out / "equity.csv", index=False)

    neighbors = []
    for name, step in STEPS.items():
        for sign, tag in ((-1, "m"), (1, "p")):
            p = {**params, name: params[name] + sign * step}
            _, nt = backtest(ohlc, p)
            nid = f"{name}_{tag}"
            nt.to_csv(out / "sensitivity" / f"trades_{nid}.csv", index=False)
            neighbors.append({"id": nid, "params": p})
    (out / "sensitivity" / "grid.json").write_text(json.dumps(
        {"base": {"id": "base", "params": params}, "steps": STEPS, "neighbors": neighbors}, indent=2))

    manifest = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": tree_sha256(STRATEGY_DIR),
        "builder_id": builder_id,
        "strategy_type": "long_only",
        "initial_capital": INIT_CASH,
        "currency": "USD",
        "benchmark_symbol": "BTC-USD",
        "contract_multiplier": 1,
        "params": params,
        "n_trials": n_trials,
        "backtest_start": _iso(idx[:1])[0],
        "backtest_end": _iso(idx[-1:], day_end)[0],
        "settings": {"venue_fees_enabled": True, "fee_bps": FEE_BPS, "slippage_bps": SLIPPAGE_BPS},
        # vectorbt's own claims. The gate never uses these for decisions; it checks them against the trades.
        "summary": {"sharpe": float(pf.sharpe_ratio()), "max_drawdown": float(abs(pf.max_drawdown())),
                    "total_return": float(pf.total_return()), "n_trades": int(len(trades))},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out


def load_ohlc(path: Path = DATA) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True))
    return df[["open", "close"]].astype(float)


def main() -> int:
    if not DATA.exists():
        print("no data: run python research/fetch_btc.py first", file=sys.stderr)
        return 1
    params = json.loads((STRATEGY_DIR / "params.json").read_text())
    for name, step in STEPS.items():
        for sign in (0, -1, 1):
            record_trial(TRIALS, STRATEGY_ID, {**params, name: params[name] + sign * step})
    n_trials = count_trials(TRIALS, STRATEGY_ID)
    version = tree_sha256(STRATEGY_DIR)
    out = build_bundle(load_ohlc(), OUTPUT / version, builder_id="soumik34343434434", n_trials=n_trials)
    print(f"bundle: {out.relative_to(ROOT)}  (strategy_version {version[:12]}..., n_trials {n_trials})")
    print(f"next:   python -I gate_check.py {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
