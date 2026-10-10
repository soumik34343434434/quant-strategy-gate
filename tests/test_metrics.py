"""Metric definitions from SPEC §4, checked against hand-computed values."""
import math
import re

import numpy as np
import pandas as pd
import pytest
from scipy.stats import kurtosis, norm, skew

from gatecheck import metrics as M
from gatecheck.errors import MetricError
from tests.fixtures.helpers import ROOT


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_net_pnl_subtracts_every_cost_and_applies_stress_multipliers():
    t = pd.DataFrame({"gross_pnl": [100.0, -50.0], "fees": [2.0, 1.0], "slippage": [3.0, 1.0],
                      "financing": [1.0, 0.0]})
    assert list(M.net_pnl(t)) == [94.0, -52.0]
    assert list(M.net_pnl(t, fee_mult=2.0, slip_mult=2.0)) == [89.0, -54.0]


def test_daily_pnl_books_on_exit_date_and_rolls_to_next_calendar_day():
    cal = pd.DatetimeIndex([ts("2024-01-01"), ts("2024-01-02"), ts("2024-01-05")])
    exits = pd.Series([ts("2024-01-01T10:00"), ts("2024-01-02T23:00"), ts("2024-01-03T09:00"),
                       ts("2024-01-04T09:00")])
    out = M.daily_pnl(exits, np.array([1.0, 2.0, 3.0, 4.0]), cal)
    assert list(out.index) == list(cal)
    assert list(out) == [1.0, 2.0, 7.0]


def test_returns_from_pnl_compound_on_prior_equity():
    r, eq = M.returns_from_pnl(pd.Series([10.0, -5.0]), 100.0)
    assert list(eq) == [110.0, 105.0]
    assert r.iloc[0] == pytest.approx(0.1)
    assert r.iloc[1] == pytest.approx(-5.0 / 110.0)


def test_returns_from_pnl_refuses_ruin():
    with pytest.raises(MetricError):
        M.returns_from_pnl(pd.Series([-100.0, 5.0]), 100.0)


def test_returns_from_equity_uses_initial_capital_as_first_prior():
    r = M.returns_from_equity(pd.Series([110.0, 99.0]), 100.0)
    assert list(r) == pytest.approx([0.1, -0.1])


def test_sharpe_hand_computed():
    assert M.sharpe(pd.Series([0.01, 0.02, 0.03]), 365) == pytest.approx(2.0 * math.sqrt(365))


@pytest.mark.parametrize("r", [[0.01, 0.01, 0.01], [0.01], [], [0.01, float("nan"), 0.02]])
def test_sharpe_not_computable(r):
    with pytest.raises(MetricError):
        M.sharpe(pd.Series(r, dtype=float), 365)


def test_profit_factor():
    assert M.profit_factor(np.array([10.0, -5.0, 20.0, -10.0])) == pytest.approx(2.0)
    assert M.profit_factor(np.array([-1.0, -2.0])) == 0.0
    with pytest.raises(MetricError):
        M.profit_factor(np.array([1.0, 2.0]))


def test_max_drawdown_includes_initial_capital_as_a_peak():
    assert M.max_drawdown(pd.Series([120.0, 90.0, 130.0, 104.0]), 100.0) == pytest.approx(0.25)
    assert M.max_drawdown(pd.Series([90.0, 95.0]), 100.0) == pytest.approx(0.10)
    assert M.max_drawdown(pd.Series([50.0, -10.0]), 100.0) == 1.0


def test_buy_and_hold_pays_one_round_trip():
    closes = pd.Series([100.0, 120.0, 150.0])
    assert M.buy_and_hold(closes, 0.001) == pytest.approx(0.999 * 1.5 * 0.999 - 1)


def test_beta_and_up_down_beta():
    x = np.array([0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.015, -0.005])
    assert M.beta(0.5 * x + 0.001, x) == pytest.approx(0.5)
    up, down = M.up_down_beta(0.3 * np.abs(x), x)
    assert up == pytest.approx(0.3)
    assert down == pytest.approx(-0.3)


def test_beta_not_computable_without_variance():
    with pytest.raises(MetricError):
        M.beta(np.array([0.1, 0.2, 0.3]), np.array([0.01, 0.01, 0.01]))


def test_deflated_sharpe_single_trial_is_probabilistic_sharpe():
    rng = np.random.default_rng(1)
    r = pd.Series(0.001 + 0.01 * rng.standard_normal(500))
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = skew(r), kurtosis(r, fisher=False)
    expected = norm.cdf(sr * math.sqrt(len(r) - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2))
    assert M.deflated_sharpe(r, 1) == pytest.approx(expected, rel=1e-9)


def test_deflated_sharpe_falls_as_trials_rise():
    rng = np.random.default_rng(2)
    r = pd.Series(0.002 + 0.01 * rng.standard_normal(750))
    vals = [M.deflated_sharpe(r, n) for n in (1, 10, 100, 10_000)]
    assert vals == sorted(vals, reverse=True)
    assert vals[0] > vals[-1]


def test_deflated_sharpe_hand_computed_threshold():
    rng = np.random.default_rng(3)
    r = pd.Series(0.001 + 0.01 * rng.standard_normal(1000))
    n, t = 50, len(r)
    em = 0.5772156649015329
    sr0 = math.sqrt(1 / (t - 1)) * ((1 - em) * norm.ppf(1 - 1 / n) + em * norm.ppf(1 - 1 / (n * math.e)))
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = skew(r), kurtosis(r, fisher=False)
    expected = norm.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2))
    assert M.deflated_sharpe(r, n) == pytest.approx(expected, rel=1e-9)


def test_metrics_module_is_pure():
    src = (ROOT / "gatecheck" / "metrics.py").read_text()
    imports = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M))
    assert imports <= {"__future__", "math", "numpy", "pandas", "scipy.stats", "gatecheck.errors", ".errors"}
    assert "open(" not in src and "summary" not in src
