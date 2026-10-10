"""Layer 2: pure metric functions (SPEC §4).

No file access, no thresholds, and never the report's self-reported numbers.
Anything that cannot be computed raises MetricError; callers treat that as a fail.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

from .errors import MetricError

EULER_GAMMA = 0.5772156649015329
_TINY = 1e-12


def net_pnl(trades: pd.DataFrame, fee_mult: float = 1.0, slip_mult: float = 1.0) -> pd.Series:
    return (trades["gross_pnl"] - fee_mult * trades["fees"] - slip_mult * trades["slippage"]
            - trades["financing"])


def daily_closes(bench: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Last close per UTC date, restricted to dates inside [start, end]."""
    s = bench.set_index(bench["timestamp"].dt.normalize())["close"]
    s = s.groupby(level=0).last()
    return s[(s.index >= start.normalize()) & (s.index <= end.normalize())]


def daily_pnl(exit_times: pd.Series, nets, calendar: pd.DatetimeIndex) -> pd.Series:
    """Book each trade's net P&L on its exit date; non-calendar dates roll to the next
    calendar date (exits after the last calendar date land on the last one)."""
    if len(calendar) == 0:
        raise MetricError("empty calendar")
    days = pd.DatetimeIndex(pd.Series(exit_times).dt.normalize())
    pos = np.minimum(calendar.searchsorted(days, side="left"), len(calendar) - 1)
    out = np.zeros(len(calendar))
    np.add.at(out, pos, np.asarray(nets, dtype=float))
    return pd.Series(out, index=calendar)


def returns_from_pnl(daily: pd.Series, initial: float) -> tuple[pd.Series, pd.Series]:
    equity = initial + daily.cumsum()
    if (equity <= 0).any():
        raise MetricError("equity reaches zero or below (ruin)")
    prior = equity.shift(1)
    prior.iloc[0] = initial
    return daily / prior, equity


def equity_on_calendar(equity: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.Series:
    """Equity at the end of each calendar date (last observation on or before it)."""
    s = equity.set_index(equity["timestamp"].dt.normalize())["equity"].groupby(level=0).last()
    s = s.reindex(s.index.union(calendar)).ffill()
    out = s.reindex(calendar)
    if out.isna().any():
        raise MetricError("equity curve does not cover the calendar")
    return out


def returns_from_equity(equity: pd.Series, initial: float) -> pd.Series:
    if (equity <= 0).any() or initial <= 0:
        raise MetricError("equity reaches zero or below (ruin)")
    prior = equity.shift(1)
    prior.iloc[0] = initial
    return equity / prior - 1


def _finite(x, what: str) -> np.ndarray:
    a = np.asarray(x, dtype=float)
    if not np.all(np.isfinite(a)):
        raise MetricError(f"{what} contains non-finite values")
    return a


def sharpe(returns, periods_per_year: int) -> float:
    r = _finite(returns, "returns")
    if len(r) < 2:
        raise MetricError("fewer than 2 return observations")
    sd = r.std(ddof=1)
    if not sd > _TINY:
        raise MetricError("returns have zero variance")
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def profit_factor(nets) -> float:
    n = _finite(nets, "trade P&L")
    wins = n[n > 0].sum()
    losses = -n[n < 0].sum()
    if not losses > 0:
        raise MetricError("no losing trades, profit factor undefined")
    return float(wins / losses)


def max_drawdown(equity, initial: float) -> float:
    curve = np.concatenate([[initial], _finite(equity, "equity")])
    if (curve <= 0).any():
        return 1.0
    peak = np.maximum.accumulate(curve)
    return float(((peak - curve) / peak).max())


def total_return(nets, initial: float) -> float:
    return float(_finite(nets, "trade P&L").sum() / initial)


def buy_and_hold(closes, cost_per_side: float) -> float:
    c = _finite(closes, "benchmark closes")
    if len(c) < 2 or (c <= 0).any():
        raise MetricError("need at least two positive benchmark closes")
    return float((1 - cost_per_side) * (c[-1] / c[0]) * (1 - cost_per_side) - 1)


def beta(y, x) -> float:
    y = _finite(y, "strategy returns")
    x = _finite(x, "benchmark returns")
    if len(x) != len(y) or len(x) < 3:
        raise MetricError("need at least 3 aligned observations")
    xm = x - x.mean()
    sxx = xm @ xm
    if not sxx > _TINY ** 2 * len(x):
        raise MetricError("benchmark returns have zero variance")
    return float(xm @ (y - y.mean()) / sxx)


def up_down_beta(y, x) -> tuple[float, float]:
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    up, down = x > 0, x < 0
    return beta(y[up], x[up]), beta(y[down], x[down])


def deflated_sharpe(returns, n_trials: int) -> float:
    """Bailey & Lopez de Prado deflated Sharpe ratio on per-period returns."""
    r = _finite(returns, "returns")
    t = len(r)
    if t < 3:
        raise MetricError("fewer than 3 return observations")
    if not (isinstance(n_trials, int) and n_trials >= 1):
        raise MetricError("n_trials must be a positive integer")
    sd = r.std(ddof=1)
    if not sd > _TINY:
        raise MetricError("returns have zero variance")
    sr = r.mean() / sd
    g3 = float(skew(r))
    g4 = float(kurtosis(r, fisher=False))
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(1 / (t - 1)) * ((1 - EULER_GAMMA) * norm.isf(1 / n_trials)
                                         + EULER_GAMMA * norm.isf(1 / (n_trials * math.e)))
    var_term = 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2
    if not var_term > 0:
        raise MetricError("deflated Sharpe undefined for this return distribution")
    return float(norm.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(var_term)))
