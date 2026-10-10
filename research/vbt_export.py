"""vectorbt trade records -> canonical trades.csv rows.

vectorbt applies slippage inside the fill price (buy at p*(1+s), sell at p*(1-s))
and reports PnL net of fees. The gate wants slippage as its own column so it can
stress it, so this splits it back out using the same rate the backtest used:

  entry/exit price = the unslipped price
  slippage         = size * |fill - unslipped|, both legs
  gross_pnl        = side * size * (exit - entry)

and then checks that gross - fees - slippage equals vectorbt's own PnL.
Nothing is filled in or defaulted; open trades are refused.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CANONICAL_COLS = ["trade_id", "entry_time", "exit_time", "side", "size", "entry_price", "exit_price",
                  "gross_pnl", "fees", "slippage", "financing"]


def _iso(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_canonical(records: pd.DataFrame, slippage: float, multiplier: float = 1.0) -> pd.DataFrame:
    if (records["Status"] != "Closed").any():
        raise ValueError("open trades in the export: close every position before the backtest ends")
    s = float(slippage)
    long = (records["Direction"] == "Long").to_numpy()
    size = records["Size"].to_numpy(dtype=float)
    fill_in = records["Avg Entry Price"].to_numpy(dtype=float)
    fill_out = records["Avg Exit Price"].to_numpy(dtype=float)
    entry = np.where(long, fill_in / (1 + s), fill_in / (1 - s))
    exit_ = np.where(long, fill_out / (1 - s), fill_out / (1 + s))
    slip = size * (np.abs(fill_in - entry) + np.abs(fill_out - exit_)) * multiplier
    gross = np.where(long, 1.0, -1.0) * size * (exit_ - entry) * multiplier
    fees = (records["Entry Fees"] + records["Exit Fees"]).to_numpy(dtype=float)
    net = gross - fees - slip
    if not np.allclose(net, records["PnL"].to_numpy(dtype=float), rtol=1e-9, atol=1e-6):
        raise ValueError("cost split does not reproduce the backtester's own P&L")
    return pd.DataFrame({
        "trade_id": [f"T{int(i):05d}" for i in records["Exit Trade Id"]],
        "entry_time": _iso(records["Entry Timestamp"]).to_numpy(),
        "exit_time": _iso(records["Exit Timestamp"]).to_numpy(),
        "side": np.where(long, "long", "short"),
        "size": size,
        "entry_price": entry,
        "exit_price": exit_,
        "gross_pnl": gross,
        "fees": fees,
        "slippage": slip,
        "financing": np.zeros(len(size)),  # spot: no funding or borrow
    }, columns=CANONICAL_COLS)
