"""Short-term mean reversion ("buy the dip") in an uptrend, long/flat.

Enter when the close has fallen more than drop_pct over drop_days while it is
still above its trend_ma-day average. Exit when the close recovers above its
exit_ma-day average. Signals use the close of day t and execute at the open of
day t+1, so no order ever uses a price it could not have seen.

Every file in this directory (this one and params.json) is hashed into the
strategy_version. Any edit is a new version and must be re-checked from scratch.
"""
import pandas as pd


def _pos_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def signals(close: pd.Series, trend_ma: int, drop_days: int, drop_pct: float,
            exit_ma: int) -> tuple[pd.Series, pd.Series]:
    """Returns (entries, exits) aligned to the bar on whose open they execute."""
    if not (_pos_int(trend_ma) and _pos_int(drop_days) and _pos_int(exit_ma)):
        raise ValueError("trend_ma, drop_days and exit_ma must be positive integers")
    if isinstance(drop_pct, bool) or not isinstance(drop_pct, (int, float)) or not 0 < drop_pct < 1:
        raise ValueError("drop_pct must be a fraction between 0 and 1")
    uptrend = close > close.rolling(trend_ma).mean()
    dipped = close / close.shift(drop_days) - 1 < -drop_pct
    exits = close > close.rolling(exit_ma).mean()
    entries = uptrend & dipped & ~exits
    return entries.shift(1, fill_value=False), exits.shift(1, fill_value=False)
