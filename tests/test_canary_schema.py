"""Missing fields, wrong types, corrupted files: all must block at gate 0."""
import json

import pandas as pd
import pytest

from tests.fixtures.builder import edit_csv, edit_manifest
from tests.fixtures.helpers import detail, failed, reason, status

TRADE_COLS = ["trade_id", "entry_time", "exit_time", "side", "size", "entry_price", "exit_price",
              "gross_pnl", "fees", "slippage", "financing"]
MANIFEST_FIELDS = ["strategy_id", "strategy_version", "builder_id", "strategy_type", "initial_capital",
                   "currency", "benchmark_symbol", "contract_multiplier", "params", "n_trials",
                   "backtest_start", "backtest_end", "settings"]
SETTINGS_FIELDS = ["venue_fees_enabled", "fee_bps", "slippage_bps"]


def assert_integrity_block(v, needle: str, skipped: bool = True):
    assert v.verdict == "BLOCKED"
    assert v.exit_code == 1
    assert failed(v) == {0}, detail(v)
    assert needle.lower() in reason(v, 0).lower(), reason(v, 0)
    if skipped:
        assert all(status(v, g) == "SKIPPED" for g in range(1, 13)), detail(v)


@pytest.mark.parametrize("col", TRADE_COLS)
def test_missing_trade_column(col, clean, check):
    edit_csv(clean.path / "trades.csv", lambda df: df.drop(columns=[col]))
    assert_integrity_block(check(clean.path), col)


@pytest.mark.parametrize("file,col", [("benchmark.csv", "timestamp"), ("benchmark.csv", "close"),
                                      ("equity.csv", "timestamp"), ("equity.csv", "equity")])
def test_missing_series_column(file, col, clean, check):
    edit_csv(clean.path / file, lambda df: df.drop(columns=[col]))
    assert_integrity_block(check(clean.path), col)


@pytest.mark.parametrize("field", MANIFEST_FIELDS)
def test_missing_manifest_field(field, clean, check):
    edit_manifest(clean.path, lambda m: m.pop(field))
    assert_integrity_block(check(clean.path), field)


@pytest.mark.parametrize("field", SETTINGS_FIELDS)
def test_missing_settings_field(field, clean, check):
    edit_manifest(clean.path, lambda m: m["settings"].pop(field))
    assert_integrity_block(check(clean.path), field)


@pytest.mark.parametrize("name", ["manifest.json", "trades.csv", "benchmark.csv", "equity.csv"])
def test_missing_core_file(name, clean, check):
    (clean.path / name).unlink()
    assert_integrity_block(check(clean.path), name)


def _set(col, row, value):
    def fn(df):
        df.loc[row, col] = value
        return df
    return fn


def _shift_gross(df):
    df.loc[10, "gross_pnl"] = str(float(df.loc[10, "gross_pnl"]) + 50.0)
    return df


def _dup_id(df):
    df.loc[1, "trade_id"] = df.loc[0, "trade_id"]
    return df


def _exit_before_entry(df):
    entry = pd.Timestamp(df.loc[4, "entry_time"])
    df.loc[4, "exit_time"] = (entry - pd.Timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return df


TRADE_VALUE_CASES = [
    ("text in numeric", _set("size", 3, "abc"), "size"),
    ("bad date", _set("entry_time", 5, "2022-13-45T00:00:00Z"), "entry_time"),
    ("naive date", _set("exit_time", 5, "2022-01-06T15:00:00"), "timezone"),
    ("empty cell", _set("fees", 2, ""), "fees"),
    ("nan literal", _set("fees", 2, "nan"), "fees"),
    ("inf", _set("gross_pnl", 2, "inf"), "gross_pnl"),
    ("negative fee", _set("fees", 1, "-1.0"), "fees"),
    ("negative financing", _set("financing", 1, "-3.0"), "financing"),
    ("zero size", _set("size", 1, "0"), "size"),
    ("bad side", _set("side", 0, "LONG"), "side"),
    ("gross mismatch", _shift_gross, "gross_pnl"),
    ("duplicate id", _dup_id, "trade_id"),
    ("outside window", _set("entry_time", 0, "2021-12-01T00:00:00Z"), "window"),
    ("exit before entry", _exit_before_entry, "exit_time"),
]


@pytest.mark.parametrize("label,mutate,needle", TRADE_VALUE_CASES, ids=[c[0] for c in TRADE_VALUE_CASES])
def test_bad_trade_values(label, mutate, needle, clean, check):
    edit_csv(clean.path / "trades.csv", mutate)
    assert_integrity_block(check(clean.path), needle)


@pytest.mark.parametrize("content,label", [
    (b"", "empty"),
    (b"trade_id,entry_time,exit_time,side,size,entry_price,exit_price,gross_pnl,fees,slippage,financing\n", "header only"),
    (b"\x00\xff\xfe\x89PNG\r\n\x1a\n\x00\x00garbage", "binary"),
])
def test_unusable_trades_file(content, label, clean, check):
    (clean.path / "trades.csv").write_bytes(content)
    assert_integrity_block(check(clean.path), "trades.csv")


def test_truncated_trades_file(clean, check):
    p = clean.path / "trades.csv"
    data = p.read_bytes()
    p.write_bytes(data[: len(data) // 2])  # cuts a row in half
    assert_integrity_block(check(clean.path), "trades.csv")


def test_csv_with_too_many_fields(clean, check):
    p = clean.path / "trades.csv"
    p.write_text(p.read_text() + "x,1,2,3,4,5,6,7,8,9,10,11,12\n")
    assert_integrity_block(check(clean.path), "trades.csv")


MANIFEST_VALUE_CASES = [
    ("bool as number", lambda m: m.update(initial_capital=True), "initial_capital"),
    ("string as number", lambda m: m.update(initial_capital="100000"), "initial_capital"),
    ("negative capital", lambda m: m.update(initial_capital=-1), "initial_capital"),
    ("version not hex", lambda m: m.update(strategy_version="xyz"), "strategy_version"),
    ("unknown strategy type", lambda m: m.update(strategy_type="market_neutral"), "strategy_type"),
    ("unknown benchmark", lambda m: m.update(benchmark_symbol="DOGE-USD"), "benchmark"),
    ("start after end", lambda m: m.update(backtest_start="2025-06-01T00:00:00Z"), "backtest"),
    ("naive start", lambda m: m.update(backtest_start="2022-01-01T00:00:00"), "timezone"),
    ("params not numeric", lambda m: m.update(params={"fast": "ten", "slow": 50}), "params"),
    ("params bool", lambda m: m.update(params={"fast": True, "slow": 50}), "params"),
    ("n_trials zero", lambda m: m.update(n_trials=0), "n_trials"),
    ("n_trials float", lambda m: m.update(n_trials=2.5), "n_trials"),
    ("multiplier zero", lambda m: m.update(contract_multiplier=0), "contract_multiplier"),
    ("fees flag not bool", lambda m: m["settings"].update(venue_fees_enabled="yes"), "venue_fees_enabled"),
    ("negative slippage", lambda m: m["settings"].update(slippage_bps=-1), "slippage_bps"),
    ("settings not object", lambda m: m.update(settings=[1, 2]), "settings"),
]


@pytest.mark.parametrize("label,mutate,needle", MANIFEST_VALUE_CASES, ids=[c[0] for c in MANIFEST_VALUE_CASES])
def test_bad_manifest_values(label, mutate, needle, clean, check):
    edit_manifest(clean.path, mutate)
    assert_integrity_block(check(clean.path), needle)


@pytest.mark.parametrize("text,label", [("{not json", "syntax"), ('{"initial_capital": NaN}', "NaN"),
                                         ("[1, 2, 3]", "not an object")])
def test_unparseable_manifest(text, label, clean, check):
    (clean.path / "manifest.json").write_text(text)
    assert_integrity_block(check(clean.path), "manifest")


def test_equity_curve_that_does_not_reconcile(clean, check):
    def bump_last(df):
        df.loc[len(df) - 1, "equity"] = str(float(df.loc[len(df) - 1, "equity"]) + 5000)
        return df
    edit_csv(clean.path / "equity.csv", bump_last)
    assert_integrity_block(check(clean.path), "equity")


def test_equity_curve_wrong_starting_capital(clean, check):
    edit_csv(clean.path / "equity.csv", _set("equity", 0, "90000"))
    assert_integrity_block(check(clean.path), "equity")


def test_equity_curve_out_of_order(clean, check):
    def swap(df):
        df.loc[[10, 11]] = df.loc[[11, 10]].to_numpy()
        return df
    edit_csv(clean.path / "equity.csv", swap)
    assert_integrity_block(check(clean.path), "equity")


def test_benchmark_gap(clean, check):
    edit_csv(clean.path / "benchmark.csv", lambda df: df.drop(index=range(400, 410)).reset_index(drop=True))
    assert_integrity_block(check(clean.path), "gap")


def test_benchmark_does_not_cover_window(clean, check):
    edit_csv(clean.path / "benchmark.csv", lambda df: df.iloc[:-30].reset_index(drop=True))
    assert_integrity_block(check(clean.path), "cover")


def test_benchmark_non_positive_price(clean, check):
    edit_csv(clean.path / "benchmark.csv", _set("close", 50, "0"))
    assert_integrity_block(check(clean.path), "close")


def test_declared_long_only_with_short_trades(clean, check):
    edit_manifest(clean.path, lambda m: m.update(strategy_type="long_only"))
    v = check(clean.path)
    assert_integrity_block(v, "short", skipped=False)
