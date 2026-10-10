"""Research pipeline: no look-ahead, exact cost accounting, bundles the gate can read."""
import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.helpers import ROOT, detail, status

S, FEE = 0.0005, 0.001


STRATEGY_NAMES = ["ma_trend", "dip_buy"]


def load_strategy(name="ma_trend"):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"research/strategies/{name}/strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def params_of(name):
    return json.loads((ROOT / f"research/strategies/{name}/params.json").read_text())


def random_walk(n=900, seed=3, drift=0.0005, vol=0.03, start="2021-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.Series(20_000 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n))), index=idx)


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


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_signals_never_use_future_prices(name):
    """Changing the close on day k must not change any signal on or before day k."""
    strat, params = load_strategy(name), params_of(name)
    close = random_walk(400, seed=0, vol=0.02, start="2020-01-01")
    e1, x1 = strat.signals(close, **params)
    for k in (120, 250, 399):
        bumped = close.copy()
        bumped.iloc[k] *= 1.5 if k != 250 else 0.6
        e2, x2 = strat.signals(bumped, **params)
        assert e1.iloc[: k + 1].equals(e2.iloc[: k + 1]), k
        assert x1.iloc[: k + 1].equals(x2.iloc[: k + 1]), k


def test_dip_buy_only_buys_dips_in_an_uptrend():
    strat, p = load_strategy("dip_buy"), params_of("dip_buy")
    close = random_walk(1500, seed=5)
    entries, exits = strat.signals(close, **p)
    assert entries.sum() > 10, "fixture should produce entries"
    # an entry on bar t+1 comes from the close of bar t
    prev = close.shift(1)
    trend = close.rolling(p["trend_ma"]).mean().shift(1)
    drop = (close / close.shift(p["drop_days"]) - 1).shift(1)
    assert (prev[entries] > trend[entries]).all()
    assert (drop[entries] < -p["drop_pct"]).all()
    assert not (entries & exits).any()


@pytest.mark.parametrize("bad", [{"trend_ma": 0}, {"drop_days": 0}, {"drop_pct": -0.01}, {"exit_ma": 2.5},
                                 {"drop_pct": True}])
def test_dip_buy_rejects_invalid_params(bad):
    strat, p = load_strategy("dip_buy"), params_of("dip_buy")
    with pytest.raises(ValueError):
        strat.signals(random_walk(300), **{**p, **bad})


def test_every_strategy_declares_a_step_for_each_param():
    from research.run_strategy import STRATEGIES
    assert set(STRATEGIES) == set(STRATEGY_NAMES)
    for name, spec in STRATEGIES.items():
        assert set(spec["steps"]) == set(params_of(name)), name


def test_trials_log_counts_distinct_parameter_sets(tmp_path):
    from research.trials import count_trials, record_trial
    log = tmp_path / "trials.jsonl"
    for p in ({"fast": 20, "slow": 100}, {"fast": 15, "slow": 100}, {"fast": 20, "slow": 100}):
        record_trial(log, "ma_trend", p)
    record_trial(log, "other", {"fast": 20, "slow": 100})
    assert count_trials(log, "ma_trend") == 2


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_runner_bundle_is_readable_by_the_gate(name, tmp_path, test_config):
    pytest.importorskip("vectorbt")
    from gatecheck.runner import run_check
    from research.run_strategy import build_bundle

    close = random_walk()
    ohlc = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "close": close})
    bundle = build_bundle(name, ohlc, tmp_path / "bundle", builder_id="test-builder", n_trials=5)
    v = run_check(bundle, test_config)
    assert status(v, 0) == "PASS", detail(v)  # schema, reconciliation, honest summary
    assert status(v, 1) == "PASS", detail(v)  # costs declared and actually charged
    assert status(v, 6) == "N/A", detail(v)   # long-only book
    assert status(v, 7) == "FAIL"             # no independent risk review: never written by the builder
    assert status(v, 11) == "FAIL"            # no paper period yet
    assert v.verdict == "BLOCKED"
