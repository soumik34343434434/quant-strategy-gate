"""Strict, fail-closed parsing of a report bundle (SPEC §3).

Everything in the bundle is untrusted data. CSVs are read as text and
converted column by column; any missing, malformed or inconsistent value
raises InputError naming the file, row and column. Nothing defaults.
"""
from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import metrics
from .errors import InputError

TRADE_COLS = ["trade_id", "entry_time", "exit_time", "side", "size", "entry_price", "exit_price",
              "gross_pnl", "fees", "slippage", "financing"]
MANIFEST_FIELDS = ["strategy_id", "strategy_version", "builder_id", "strategy_type", "initial_capital",
                   "currency", "benchmark_symbol", "contract_multiplier", "params", "n_trials",
                   "backtest_start", "backtest_end", "settings"]
SETTINGS_FIELDS = ["venue_fees_enabled", "fee_bps", "slippage_bps"]
STRATEGY_TYPES = {"long_only", "short_only", "long_short"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Core:
    root: Path
    manifest: dict
    trades: pd.DataFrame
    benchmark: pd.DataFrame
    equity: pd.DataFrame
    start: pd.Timestamp
    end: pd.Timestamp
    calendar: pd.DatetimeIndex
    closes: pd.Series
    periods_per_year: int


# ------------------------------------------------------------------ primitives

def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name} is not allowed")


def read_json(path: Path, label: str):
    if not path.is_file():
        raise InputError(f"{label} is missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except Exception as e:
        raise InputError(f"{label} could not be parsed: {e}") from e


def parse_ts(value, label: str) -> pd.Timestamp:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{label}: expected an ISO-8601 timestamp, got {value!r}")
    try:
        d = datetime.fromisoformat(value.strip())
    except ValueError as e:
        raise InputError(f"{label}: {value!r} is not an ISO-8601 timestamp") from e
    if d.tzinfo is None or d.utcoffset() is None:
        raise InputError(f"{label}: {value!r} has no timezone")
    return pd.Timestamp(d).tz_convert("UTC")


def read_csv(path: Path, label: str, required: list[str]) -> pd.DataFrame:
    if not path.is_file():
        raise InputError(f"{label} is missing")
    raw = path.read_bytes()
    if not raw.strip():
        raise InputError(f"{label} is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InputError(f"{label} is not UTF-8 text") from e
    if "\x00" in text:
        raise InputError(f"{label} contains NUL bytes")
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, na_filter=False,
                         on_bad_lines="error")
    except Exception as e:
        raise InputError(f"{label} could not be parsed: {e}") from e
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise InputError(f"{label} is missing column(s): {', '.join(missing)}")
    if len(df) == 0:
        raise InputError(f"{label} has no data rows")
    return df


def _cells(df: pd.DataFrame, col: str, label: str) -> list[str]:
    out = []
    for i, v in enumerate(df[col].tolist()):
        if v is None or (isinstance(v, float) and math.isnan(v)) or not str(v).strip():
            raise InputError(f"{label} row {i + 2} column {col}: empty value")
        out.append(str(v).strip())
    return out


def num_col(df, col, label, *, minimum=None, strictly_positive=False) -> np.ndarray:
    vals = _cells(df, col, label)
    out = np.empty(len(vals))
    for i, v in enumerate(vals):
        try:
            x = float(v)
        except ValueError:
            raise InputError(f"{label} row {i + 2} column {col}: {v!r} is not a number") from None
        if not math.isfinite(x):
            raise InputError(f"{label} row {i + 2} column {col}: {v!r} is not a finite number")
        if strictly_positive and not x > 0:
            raise InputError(f"{label} row {i + 2} column {col}: {v!r} must be > 0")
        if minimum is not None and x < minimum:
            raise InputError(f"{label} row {i + 2} column {col}: {v!r} must be >= {minimum}")
        out[i] = x
    return out


def ts_col(df, col, label) -> pd.Series:
    vals = _cells(df, col, label)
    return pd.Series([parse_ts(v, f"{label} row {i + 2} column {col}") for i, v in enumerate(vals)],
                     dtype="datetime64[ns, UTC]")


# ------------------------------------------------------------------ manifest

def load_manifest(root: Path) -> dict:
    m = read_json(root / "manifest.json", "manifest.json")
    if not isinstance(m, dict):
        raise InputError("manifest.json must be a JSON object")
    missing = [f for f in MANIFEST_FIELDS if f not in m]
    if missing:
        raise InputError(f"manifest.json is missing field(s): {', '.join(missing)}")
    errs = []

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    for f in ("strategy_id", "builder_id", "currency", "benchmark_symbol"):
        need(isinstance(m[f], str) and m[f].strip(), f"{f} must be a non-empty string")
    need(isinstance(m["strategy_version"], str) and HEX64.match(m["strategy_version"]),
         "strategy_version must be 64 lowercase hex characters")
    need(m["strategy_type"] in STRATEGY_TYPES, f"strategy_type must be one of {sorted(STRATEGY_TYPES)}")
    need(_is_num(m["initial_capital"]) and m["initial_capital"] > 0, "initial_capital must be a number > 0")
    need(_is_num(m["contract_multiplier"]) and m["contract_multiplier"] > 0,
         "contract_multiplier must be a number > 0")
    need(isinstance(m["n_trials"], int) and not isinstance(m["n_trials"], bool) and m["n_trials"] >= 1,
         "n_trials must be an integer >= 1")
    p = m["params"]
    need(isinstance(p, dict) and all(isinstance(k, str) and _is_num(v) for k, v in p.items()),
         "params must be an object of numeric values")
    s = m["settings"]
    if not isinstance(s, dict):
        errs.append("settings must be an object")
    else:
        sm = [f for f in SETTINGS_FIELDS if f not in s]
        if sm:
            errs.append(f"settings is missing field(s): {', '.join(sm)}")
        else:
            need(isinstance(s["venue_fees_enabled"], bool), "settings.venue_fees_enabled must be true or false")
            need(_is_num(s["fee_bps"]) and s["fee_bps"] >= 0, "settings.fee_bps must be a number >= 0")
            need(_is_num(s["slippage_bps"]) and s["slippage_bps"] >= 0,
                 "settings.slippage_bps must be a number >= 0")
    try:
        start = parse_ts(m["backtest_start"], "backtest_start")
        end = parse_ts(m["backtest_end"], "backtest_end")
        need(start < end, "backtest_start must be before backtest_end")
    except InputError as e:
        errs.append(str(e))
    if errs:
        raise InputError("manifest.json: " + "; ".join(errs))
    return m


# ------------------------------------------------------------------ trades

def read_trades(path: Path, label: str, *, multiplier: float, gross_tol: float,
                window: tuple[pd.Timestamp, pd.Timestamp], window_name: str = "backtest window") -> pd.DataFrame:
    df = read_csv(path, label, TRADE_COLS)
    ids = _cells(df, "trade_id", label)
    dup = pd.Series(ids).duplicated()
    if dup.any():
        i = int(np.argmax(dup.to_numpy()))
        raise InputError(f"{label} row {i + 2} column trade_id: duplicate trade_id {ids[i]!r}")
    sides = _cells(df, "side", label)
    for i, s in enumerate(sides):
        if s not in ("long", "short"):
            raise InputError(f"{label} row {i + 2} column side: {s!r} must be 'long' or 'short'")
    t = pd.DataFrame({
        "trade_id": ids,
        "entry_time": ts_col(df, "entry_time", label),
        "exit_time": ts_col(df, "exit_time", label),
        "side": sides,
        "size": num_col(df, "size", label, strictly_positive=True),
        "entry_price": num_col(df, "entry_price", label, strictly_positive=True),
        "exit_price": num_col(df, "exit_price", label, strictly_positive=True),
        "gross_pnl": num_col(df, "gross_pnl", label),
        "fees": num_col(df, "fees", label, minimum=0.0),
        "slippage": num_col(df, "slippage", label, minimum=0.0),
        "financing": num_col(df, "financing", label, minimum=0.0),
    })
    bad = t["exit_time"] < t["entry_time"]
    if bad.any():
        i = int(np.argmax(bad.to_numpy()))
        raise InputError(f"{label} row {i + 2} column exit_time: exit_time is before entry_time")
    lo, hi = window
    bad = (t["entry_time"] < lo) | (t["exit_time"] > hi)
    if bad.any():
        i = int(np.argmax(bad.to_numpy()))
        raise InputError(f"{label} row {i + 2}: trade lies outside the {window_name} [{lo}, {hi}]")
    sign = np.where(t["side"] == "long", 1.0, -1.0)
    expected = sign * t["size"] * (t["exit_price"] - t["entry_price"]) * multiplier
    tol = gross_tol * t["size"] * t["entry_price"] * multiplier + 1e-9
    bad = (t["gross_pnl"] - expected).abs() > tol
    if bad.any():
        i = int(np.argmax(bad.to_numpy()))
        raise InputError(f"{label} row {i + 2} column gross_pnl: {t['gross_pnl'].iloc[i]!r} does not match "
                         f"side x size x (exit_price - entry_price) x multiplier = {expected.iloc[i]!r}")
    return t


# ------------------------------------------------------------------ series

def read_benchmark(path: Path) -> pd.DataFrame:
    df = read_csv(path, "benchmark.csv", ["timestamp", "close"])
    b = pd.DataFrame({"timestamp": ts_col(df, "timestamp", "benchmark.csv"),
                      "close": num_col(df, "close", "benchmark.csv", strictly_positive=True)})
    if not b["timestamp"].is_monotonic_increasing or b["timestamp"].duplicated().any():
        raise InputError("benchmark.csv timestamps must be strictly increasing")
    return b


def read_equity(path: Path) -> pd.DataFrame:
    df = read_csv(path, "equity.csv", ["timestamp", "equity"])
    e = pd.DataFrame({"timestamp": ts_col(df, "timestamp", "equity.csv"),
                      "equity": num_col(df, "equity", "equity.csv")})
    if not e["timestamp"].is_monotonic_increasing or e["timestamp"].duplicated().any():
        raise InputError("equity.csv timestamps must be strictly increasing")
    return e


def _calendar(bench: pd.DataFrame, start, end, max_gap_days: float, symbol: str):
    closes = metrics.daily_closes(bench, start, end)
    if len(closes) < 2:
        raise InputError(f"benchmark.csv does not cover the backtest window for {symbol}")
    day = pd.Timedelta(days=1)
    lead = (closes.index[0] - start.normalize()) / day
    lag = (end.normalize() - closes.index[-1]) / day
    if lead > max_gap_days or lag > max_gap_days:
        raise InputError(f"benchmark.csv does not cover the backtest window "
                         f"(first close {closes.index[0].date()}, last {closes.index[-1].date()})")
    gaps = np.diff(closes.index.to_numpy()) / np.timedelta64(1, "D")
    if len(gaps) and gaps.max() > max_gap_days:
        i = int(np.argmax(gaps))
        raise InputError(f"benchmark.csv has a gap of {gaps[i]:.0f} days after {closes.index[i].date()} "
                         f"(max {max_gap_days})")
    return pd.DatetimeIndex(closes.index), closes


def load_core(root: Path, cfg) -> Core:
    root = Path(root)
    if not root.is_dir():
        raise InputError(f"report directory {root} not found")
    m = load_manifest(root)
    sym = m["benchmark_symbol"]
    bcfg = cfg.data["benchmarks"].get(sym)
    if bcfg is None:
        raise InputError(f"benchmark_symbol {sym!r} is not allowed by config {cfg.version} "
                         f"(allowed: {', '.join(sorted(cfg.data['benchmarks']))})")
    start, end = parse_ts(m["backtest_start"], "backtest_start"), parse_ts(m["backtest_end"], "backtest_end")
    integ = cfg.data["integrity"]
    problems, trades, bench, equity = [], None, None, None
    try:
        trades = read_trades(root / "trades.csv", "trades.csv", multiplier=m["contract_multiplier"],
                             gross_tol=integ["gross_pnl_rel_tol"], window=(start, end))
    except InputError as e:
        problems.append(str(e))
    try:
        bench = read_benchmark(root / "benchmark.csv")
    except InputError as e:
        problems.append(str(e))
    try:
        equity = read_equity(root / "equity.csv")
    except InputError as e:
        problems.append(str(e))
    if problems:
        raise InputError("; ".join(problems))

    calendar, closes = _calendar(bench, start, end, bcfg["max_gap_days"], sym)

    cap = float(m["initial_capital"])
    tol = integ["equity_reconcile_rel_tol"] * cap
    final = cap + float(metrics.net_pnl(trades).sum())
    gap = pd.Timedelta(days=bcfg["max_gap_days"])
    if equity["timestamp"].iloc[0] > start + gap or equity["timestamp"].iloc[-1] < end - gap:
        raise InputError("equity.csv does not cover the backtest window")
    if abs(equity["equity"].iloc[0] - cap) > tol:
        raise InputError(f"equity.csv starts at {equity['equity'].iloc[0]:.2f} but initial_capital is {cap:.2f}")
    if abs(equity["equity"].iloc[-1] - final) > tol:
        raise InputError(f"equity.csv ends at {equity['equity'].iloc[-1]:.2f} but initial_capital + net P&L "
                         f"from trades.csv is {final:.2f}")
    return Core(root=root, manifest=m, trades=trades, benchmark=bench, equity=equity, start=start, end=end,
                calendar=calendar, closes=closes, periods_per_year=int(bcfg["periods_per_year"]))


def peek_manifest(root: Path) -> dict:
    """Best-effort read for labelling outputs only; never used for decisions."""
    try:
        m = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}
