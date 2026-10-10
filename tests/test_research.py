"""Research pipeline: no look-ahead, exact cost accounting, bundles the gate can read."""
import importlib.util

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.helpers import ROOT, detail, status

S, FEE = 0.0005, 0.001


def load_strategy():
    spec = importlib.util.spec_from_file_location("ma_trend", ROOT / "research/strategies/ma_trend/strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vbt_record(direction, size, entry_mid, exit_mid, s=S, fee=FEE, status="Closed"):
    """What vectorbt reports: slipped fill prices, fees on slipped notional, PnL net of fees."""
    if direction == "Long":
        ae, ax = entry_mid * (1 + s), exit_mid * (1 - s)
        pnl = size * (ax - ae) - fee * size * (ae + ax)
    else:
        ae, ax = entry_mid * (1 - s), exit_mid * (1 + s)
        pnl = size * (ae - ax) - fee * size * (ae + ax)
    return {"Exit Trade Id": 0, "Size": size,
            "Entry Timestamp": pd.Timestamp("2024-01-02", tz="UTC"), "Avg Entry Price": ae,
            "Entry Fees": fee * size * ae, "Exit Timestamp": pd.Timestamp("2024-01-09", tz="UTC"),
            "Avg Exit Price": ax, "Exit Fees": fee * size * ax, "PnL": pnl, "Direction": direction,
            "Status": status}


@pytest.mark.parametrize("direction", ["Long", "Short"])
def test_slippage_is_split_back_out_of_fill_prices(direction):
    from research.vbt_export import CANONICAL_COLS, to_canonical
    rec = pd.DataFrame([vbt_record(direction, 2.0, 100.0, 110.0)])
    out = to_canonical(rec, slippage=S)
    assert list(out.columns) == CANONICAL_COLS
    row = out.iloc[0]
    assert row["entry_price"] == pytest.approx(100.0)
    assert row["exit_price"] == pytest.approx(110.0)
    assert row["side"] == direction.lower()
    net = row["gross_pnl"] - row["fees"] - row["slippage"] - row["financing"]
    assert net == pytest.approx(rec["PnL"].iloc[0], rel=1e-12)
    realized = row["slippage"] / (row["size"] * (row["entry_price"] + row["exit_price"]))
    assert realized == pytest.approx(S, rel=1e-12)


def test_timestamps_are_utc_iso_with_z():
    from research.vbt_export import to_canonical
    out = to_canonical(pd.DataFrame([vbt_record("Long", 1.0, 100.0, 101.0)]), slippage=S)
    assert out["entry_time"].iloc[0] == "2024-01-02T00:00:00Z"
    assert out["exit_time"].iloc[0] == "2024-01-09T00:00:00Z"


def test_open_trades_are_refused():
    from research.vbt_export import to_canonical
    with pytest.raises(ValueError, match="open"):
        to_canonical(pd.DataFrame([vbt_record("Long", 1.0, 100.0, 101.0, status="Open")]), slippage=S)


def test_signals_never_use_future_prices():
    """Changing the close on day k must not change any signal on or before day k."""
    strat = load_strategy()
    rng = np.random.default_rng(0)
    idx = pd.date_range("2020-01-01", periods=400, freq="D", tz="UTC")
    close = pd.Series(100 * np.exp(np.cumsum(0.02 * rng.standard_normal(400))), index=idx)
    e1, x1 = strat.signals(close, 20, 100)
    for k in (120, 250, 399):
        bumped = close.copy()
        bumped.iloc[k] *= 1.5
        e2, x2 = strat.signals(bumped, 20, 100)
        assert e1.iloc[: k + 1].equals(e2.iloc[: k + 1]), k
        assert x1.iloc[: k + 1].equals(x2.iloc[: k + 1]), k


def test_trials_log_counts_distinct_parameter_sets(tmp_path):
    from research.trials import count_trials, record_trial
    log = tmp_path / "trials.jsonl"
    for p in ({"fast": 20, "slow": 100}, {"fast": 15, "slow": 100}, {"fast": 20, "slow": 100}):
        record_trial(log, "ma_trend", p)
    record_trial(log, "other", {"fast": 20, "slow": 100})
    assert count_trials(log, "ma_trend") == 2


def test_runner_bundle_is_readable_by_the_gate(tmp_path, test_config):
    pytest.importorskip("vectorbt")
    from gatecheck.runner import run_check
    from research.run_ma_trend import build_bundle

    rng = np.random.default_rng(3)
    idx = pd.date_range("2021-01-01", periods=900, freq="D", tz="UTC")
    close = pd.Series(20_000 * np.exp(np.cumsum(0.0005 + 0.03 * rng.standard_normal(900))), index=idx)
    ohlc = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "close": close})
    bundle = build_bundle(ohlc, tmp_path / "bundle", builder_id="test-builder", n_trials=5)
    v = run_check(bundle, test_config)
    assert status(v, 0) == "PASS", detail(v)  # schema, reconciliation, honest summary
    assert status(v, 1) == "PASS", detail(v)  # costs declared and actually charged
    assert status(v, 6) == "N/A", detail(v)   # long-only book
    assert status(v, 7) == "FAIL"             # no independent risk review: never written by the builder
    assert status(v, 11) == "FAIL"            # no paper period yet
    assert v.verdict == "BLOCKED"
