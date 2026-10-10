"""Long/flat moving-average trend filter on daily closes.

Long while the fast SMA is above the slow SMA, flat otherwise. The signal is
computed from the close of day t and executed at the open of day t+1, so no
order ever uses a price it could not have seen.

Every file in this directory (this one and params.json) is hashed into the
strategy_version. Any edit is a new version and must be re-checked from scratch.
"""
import pandas as pd


def signals(close: pd.Series, fast: int, slow: int) -> tuple[pd.Series, pd.Series]:
    """Returns (entries, exits) aligned to the bar on whose open they execute."""
    if not (isinstance(fast, int) and isinstance(slow, int) and 0 < fast < slow):
        raise ValueError("need integer 0 < fast < slow")
    trend_up = close.rolling(fast).mean() > close.rolling(slow).mean()
    was_up = trend_up.shift(1, fill_value=False)
    entries = trend_up & ~was_up
    exits = ~trend_up & was_up
    return entries.shift(1, fill_value=False), exits.shift(1, fill_value=False)
