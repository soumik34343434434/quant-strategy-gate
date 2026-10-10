#!/usr/bin/env python3
"""Backtest a research strategy on BTC-USD with vectorbt and write a gate report bundle.

  python research/run_strategy.py <ma_trend|dip_buy>
  python -I gate_check.py research/output/<name>/<strategy_version>

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

DATA = ROOT / "research" / "data" / "btc_usd_1d.csv"
TRIALS = ROOT / "research" / "trials.jsonl"
OUTPUT = ROOT / "research" / "output"
INIT_CASH = 100_000.0
FEE_BPS, SLIPPAGE_BPS = 10.0, 5.0  # per side; set FEE_BPS to your venue's real taker fee

# Steps are the plateau grid (gate 9): every parameter is re-run one step either side.
STRATEGIES = {
    "ma_trend": {"id": "btc-ma-trend", "steps": {"fast": 5, "slow": 20}},
    "dip_buy": {"id": "btc-dip-buy", "steps": {"trend_ma": 50, "drop_days": 1, "drop_pct": 0.02, "exit_ma": 2}},
}


def strategy_dir(name: str) -> Path:
    return ROOT / "research" / "strategies" / name


def load_strategy(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_strategy", strategy_dir(name) / "strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def base_params(name: str) -> dict:
    return json.loads((strategy_dir(name) / "params.json").read_text())


def neighbor_params(name: str, params: dict) -> list[tuple[str, dict]]:
    out = []
    for key, step in STRATEGIES[name]["steps"].items():
        for sign, tag in ((-1, "m"), (1, "p")):
            out.append((f"{key}_{tag}", {**params, key: params[key] + sign * step}))
    return out


def _iso(index: pd.DatetimeIndex, offset: pd.Timedelta = pd.Timedelta(0)) -> list[str]:
    return [(t + offset).strftime("%Y-%m-%dT%H:%M:%SZ") for t in index]


def backtest(name: str, ohlc: pd.DataFrame, params: dict):
    import vectorbt as vbt

    entries, exits = load_strategy(name).signals(ohlc["close"], **params)
    entries.iloc[-1] = False
    exits.iloc[-1] = True  # close any open position at the last open: the gate takes closed trades only
    pf = vbt.Portfolio.from_signals(close=ohlc["close"], entries=entries, exits=exits, price=ohlc["open"],
                                    fees=FEE_BPS / 1e4, slippage=SLIPPAGE_BPS / 1e4, init_cash=INIT_CASH,
                                    freq="1D")
    trades = to_canonical(pf.trades.records_readable, slippage=SLIPPAGE_BPS / 1e4)
    return pf, trades


def build_bundle(name: str, ohlc: pd.DataFrame, out: Path, builder_id: str, n_trials: int,
                 params: dict | None = None) -> Path:
    params = params or base_params(name)
    if out.exists():
        shutil.rmtree(out)
    (out / "sensitivity").mkdir(parents=True)
    day_end = pd.Timedelta(hours=23, minutes=59, seconds=59)
    idx = ohlc.index

    pf, trades = backtest(name, ohlc, params)
    trades.to_csv(out / "trades.csv", index=False)
    shutil.copyfile(out / "trades.csv", out / "sensitivity" / "trades_base.csv")
    pd.DataFrame({"timestamp": _iso(idx, day_end), "close": ohlc["close"].to_numpy()}).to_csv(
        out / "benchmark.csv", index=False)
    pd.DataFrame({"timestamp": [_iso(idx[:1])[0]] + _iso(idx, day_end),
                  "equity": np.concatenate([[INIT_CASH], pf.value().to_numpy()])}).to_csv(
        out / "equity.csv", index=False)

    neighbors = []
    for nid, p in neighbor_params(name, params):
        _, nt = backtest(name, ohlc, p)
        nt.to_csv(out / "sensitivity" / f"trades_{nid}.csv", index=False)
        neighbors.append({"id": nid, "params": p})
    (out / "sensitivity" / "grid.json").write_text(json.dumps(
        {"base": {"id": "base", "params": params}, "steps": STRATEGIES[name]["steps"], "neighbors": neighbors},
        indent=2))

    manifest = {
        "strategy_id": STRATEGIES[name]["id"],
        "strategy_version": tree_sha256(strategy_dir(name)),
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


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in STRATEGIES:
        print(f"usage: python research/run_strategy.py <{'|'.join(STRATEGIES)}>", file=sys.stderr)
        return 2
    name = argv[0]
    if not DATA.exists():
        print("no data: run python research/fetch_btc.py first", file=sys.stderr)
        return 1
    params = base_params(name)
    sid = STRATEGIES[name]["id"]
    record_trial(TRIALS, sid, params)
    for _, p in neighbor_params(name, params):
        record_trial(TRIALS, sid, p)
    n_trials = count_trials(TRIALS, sid)
    version = tree_sha256(strategy_dir(name))
    out = build_bundle(name, load_ohlc(), OUTPUT / name / version, builder_id="soumik34343434434",
                       n_trials=n_trials)
    print(f"bundle: {out.relative_to(ROOT)}  (strategy_version {version[:12]}..., n_trials {n_trials})")
    print(f"next:   python -I gate_check.py {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
