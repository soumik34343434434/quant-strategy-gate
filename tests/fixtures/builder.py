"""Seeded synthetic report bundles for the canary suite.

Everything here is test data. Expected metrics are computed with plain numpy
here, independently of gatecheck.metrics, so the clean fixture also
cross-checks the metrics engine.

Trades are built backwards from a target net P&L per day, so every field
reconciles exactly (gross = sign * size * (exit - entry), costs = rate * notional).
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

PPY = 365
NAMESPACE = "strategy-gate-review"
DEFAULT_VERSION = hashlib.sha256(b"fixture-strategy-v1").hexdigest()


@dataclass(frozen=True)
class Segment:
    """Alpha returns on active days in [start, end]: exact sample mean and std."""
    start: str
    end: str
    annual_sharpe: float
    sigma: float


def yearly(annual_sharpe: float, sigma: float, years=(2022, 2023, 2024)) -> tuple:
    """One segment per calendar year, so each year's mean and std are exact."""
    return tuple(Segment(f"{y}-01-01", f"{y}-12-31", annual_sharpe, sigma) for y in years)


@dataclass(frozen=True)
class BundleSpec:
    seed: int = 7
    start: str = "2022-01-01"
    end: str = "2024-12-31"
    initial_capital: float = 100_000.0
    benchmark_symbol: str = "BTC-USD"
    strategy_type: str = "long_short"
    strategy_version: str = DEFAULT_VERSION
    builder_id: str = "builder-alice"
    reviewer_id: str = "risk-officer-1"
    review_verdict: str = "KEEP"
    review_reason: str = "drawdown profile acceptable at 5% allocation"
    n_trials: int = 20
    alpha_segments: tuple = ()  # default: yearly(4.5, 0.002)
    # (start, end, total simple return over the segment)
    bench_segments: tuple = (("2022-01-01", "2023-12-31", 0.13), ("2024-01-01", "2024-12-31", 0.062))
    bench_sigma: float = 0.01
    beta: float = 0.0
    convexity: float = 0.0
    drawdown: tuple | None = None  # (first_day, n_days, daily_return)
    active_every: int = 1
    sides: str = "alternate"  # alternate | long | short
    notional_frac: float = 0.1
    fee_bps: float = 4.0
    slippage_bps: float = 5.0
    realized_fee_bps: float | None = None
    realized_slippage_bps: float | None = None
    venue_fees_enabled: bool = True
    wash: tuple | None = None  # (start, end, fraction of equity): +X/-X trade pairs
    neighbor_ratios: tuple = (0.92, 0.88, 0.9, 0.86)
    paper_offset: float = 0.0
    paper_days: int = 35
    paper_start_delay_days: int = 1
    paper_version: str | None = None
    include_summary: bool = True
    summary_overrides: dict = field(default_factory=dict)


@dataclass
class Signer:
    key: Path
    pubkey_line: str

    def sign(self, path: Path) -> None:
        sig = path.with_name(path.name + ".sig")
        sig.unlink(missing_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.key), "-n", NAMESPACE, str(path)],
            check=True, capture_output=True,
        )


def make_signer(directory: Path, name: str) -> Signer:
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
                   check=True, capture_output=True)
    return Signer(key=key, pubkey_line=key.with_suffix(".pub").read_text().strip())


def allowed_signers_line(principal: str, signer: Signer) -> str:
    kind, blob = signer.pubkey_line.split()[:2]
    return f'{principal} namespaces="{NAMESPACE}" {kind} {blob}\n'


# ---------------------------------------------------------------- generation

def _standardized(rng: np.random.Generator, k: int) -> np.ndarray:
    z = rng.standard_normal(k)
    return (z - z.mean()) / z.std(ddof=1)


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_trade(tid, side, entry_ts, exit_ts, notional, entry_price, net, c_fee, c_slip):
    c = c_fee + c_slip
    if side == "long":
        n_exit = (net + notional * (1 + c)) / (1 - c)
        gross = n_exit - notional
    else:
        n_exit = (notional * (1 - c) - net) / (1 + c)
        gross = notional - n_exit
    assert n_exit > 0, "fixture notional too small for this P&L"
    size = notional / entry_price
    return {
        "trade_id": tid,
        "entry_time": _iso(entry_ts),
        "exit_time": _iso(exit_ts),
        "side": side,
        "size": size,
        "entry_price": entry_price,
        "exit_price": n_exit / size,
        "gross_pnl": gross,
        "fees": c_fee * (notional + n_exit),
        "slippage": c_slip * (notional + n_exit),
        "financing": 0.0,
    }


def _trades_from_daily(dates, pnl, equity_before, prices, spec: BundleSpec, prefix: str,
                       active: np.ndarray, wash_mask=None, wash_frac=0.0):
    c_fee = (spec.realized_fee_bps if spec.realized_fee_bps is not None else spec.fee_bps) / 1e4
    c_slip = (spec.realized_slippage_bps if spec.realized_slippage_bps is not None else spec.slippage_bps) / 1e4
    rows, i = [], 0
    for t, day in enumerate(dates):
        if not active[t]:
            continue
        if spec.sides == "alternate":
            side = "long" if i % 2 == 0 else "short"
        else:
            side = spec.sides
        entry = day + pd.Timedelta(hours=2 + i % 5)
        exit_ = day + pd.Timedelta(hours=14 + i % 7)
        notional = spec.notional_frac * equity_before[t]
        rows.append(_make_trade(f"{prefix}{i:05d}", side, entry, exit_, notional, prices[t], pnl[t], c_fee, c_slip))
        i += 1
        if wash_mask is not None and wash_mask[t]:
            x = wash_frac * equity_before[t]
            wn = 2 * x
            for sgn, wside in ((1, "long"), (-1, "short")):
                rows.append(_make_trade(f"{prefix}{i:05d}", wside, entry, exit_, wn, prices[t], sgn * x, c_fee, c_slip))
                i += 1
    return pd.DataFrame(rows)


def _compound(r: np.ndarray, cap: float):
    pnl = np.empty_like(r)
    before = np.empty_like(r)
    eq = cap
    for t, rt in enumerate(r):
        before[t] = eq
        pnl[t] = rt * eq
        eq += pnl[t]
    return pnl, before, before + pnl


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def expected_metrics(r: np.ndarray, equity_after: np.ndarray, nets: np.ndarray, cap: float) -> dict:
    """Plain-numpy reference values (independent of gatecheck.metrics)."""
    sharpe = r.mean() / r.std(ddof=1) * math.sqrt(PPY)
    curve = np.concatenate([[cap], equity_after])
    peak = np.maximum.accumulate(curve)
    wins, losses = nets[nets > 0].sum(), -nets[nets < 0].sum()
    return {
        "sharpe": float(sharpe),
        "profit_factor": float(wins / losses),
        "max_drawdown": float(((peak - curve) / peak).max()),
        "total_return": float((equity_after[-1] - cap) / cap),
        "net_pnl": float(nets.sum()),
        "n_trades": int(len(nets)),
    }


@dataclass
class Built:
    path: Path
    spec: BundleSpec
    expected: dict
    daily_returns: np.ndarray


def build(dest: Path, spec: BundleSpec = BundleSpec(), signer: Signer | None = None) -> Built:
    """Write a full report bundle to `dest` and return expected reference metrics."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    rng = np.random.default_rng(spec.seed)
    dates = pd.date_range(spec.start, spec.end, freq="D", tz="UTC")
    n = len(dates)
    naive = dates.tz_localize(None)

    # Benchmark: exact total return per segment, standardized noise.
    logret = np.zeros(n)
    for s, e, total in spec.bench_segments:
        idx = np.where((naive >= s) & (naive <= e) & (np.arange(n) >= 1))[0]
        logret[idx] = math.log1p(total) / len(idx) + spec.bench_sigma * _standardized(rng, len(idx))
    prices = 30_000.0 * np.exp(np.cumsum(logret))
    bench_ret = np.concatenate([[0.0], prices[1:] / prices[:-1] - 1])

    # Strategy daily returns.
    active = (np.arange(n) % spec.active_every) == 0
    r = np.zeros(n)
    for seg in spec.alpha_segments or yearly(4.5, 0.002):
        m = (naive >= seg.start) & (naive <= seg.end) & active
        mu = seg.annual_sharpe / math.sqrt(PPY) * seg.sigma
        r[m] = mu + seg.sigma * _standardized(rng, int(m.sum()))
    if spec.beta:
        r = r + spec.beta * bench_ret
    if spec.convexity:
        ab = np.abs(bench_ret[1:])
        r[1:] += spec.convexity * (ab - ab.mean())
    if spec.drawdown:
        first, k, dr = spec.drawdown
        t0 = int(np.where(naive == pd.Timestamp(first))[0][0])
        r[t0:t0 + k] = dr
    assert np.all(r[~active] == 0), "returns on inactive days need active_every=1"

    cap = spec.initial_capital
    pnl, eq_before, eq_after = _compound(r, cap)
    entry_prices = np.concatenate([[prices[0]], prices[:-1]])
    wash_mask, wash_frac = None, 0.0
    if spec.wash:
        ws, we, wash_frac = spec.wash
        wash_mask = (naive >= ws) & (naive <= we) & active
    trades = _trades_from_daily(dates, pnl, eq_before, entry_prices, spec, "T", active, wash_mask, wash_frac)
    _write_csv(trades, dest / "trades.csv")

    bench = pd.DataFrame({"timestamp": [_iso(d + pd.Timedelta(hours=23, minutes=59)) for d in dates],
                          "close": prices})
    _write_csv(bench, dest / "benchmark.csv")

    start_ts = pd.Timestamp(spec.start, tz="UTC")
    end_ts = pd.Timestamp(spec.end, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
    equity = pd.DataFrame({
        "timestamp": [_iso(start_ts)] + [_iso(d + pd.Timedelta(hours=23, minutes=59, seconds=30)) for d in dates],
        "equity": np.concatenate([[cap], eq_after]),
    })
    _write_csv(equity, dest / "equity.csv")

    expected = expected_metrics(r, eq_after, trades["gross_pnl"].to_numpy() - trades["fees"].to_numpy()
                                - trades["slippage"].to_numpy() - trades["financing"].to_numpy(), cap)

    manifest = {
        "strategy_id": "fixture-meanrev",
        "strategy_version": spec.strategy_version,
        "builder_id": spec.builder_id,
        "strategy_type": spec.strategy_type,
        "initial_capital": cap,
        "currency": "USD",
        "benchmark_symbol": spec.benchmark_symbol,
        "contract_multiplier": 1,
        "params": {"fast": 10, "slow": 50},
        "n_trials": spec.n_trials,
        "backtest_start": _iso(start_ts),
        "backtest_end": _iso(end_ts),
        "settings": {
            "venue_fees_enabled": spec.venue_fees_enabled,
            "fee_bps": spec.fee_bps,
            "slippage_bps": spec.slippage_bps,
        },
    }
    if spec.include_summary:
        manifest["summary"] = {**expected, **spec.summary_overrides}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))

    _write_sensitivity(dest, spec, r, rng, dates, entry_prices, active)
    _write_paper(dest, spec, rng, end_ts, prices[-1])
    _write_review(dest, spec, signer)
    return Built(path=dest, spec=spec, expected=expected, daily_returns=r)


def _write_sensitivity(dest, spec, r, rng, dates, prices, active):
    sens = dest / "sensitivity"
    sens.mkdir()
    shutil.copyfile(dest / "trades.csv", sens / "trades_base.csv")
    base_params = {"fast": 10, "slow": 50}
    steps = {"fast": 2, "slow": 10}
    neighbors = []
    variants = [("fast_m", {"fast": 8}), ("fast_p", {"fast": 12}), ("slow_m", {"slow": 40}), ("slow_p", {"slow": 60})]
    sigma = r.std(ddof=1)
    centered = r - r.mean()
    every_day = np.ones(len(r), dtype=bool)
    for (nid, change), ratio in zip(variants, spec.neighbor_ratios):
        z = rng.standard_normal(len(r))
        z -= z.mean()
        z -= centered * (z @ centered) / (centered @ centered)  # orthogonal to base
        z /= z.std(ddof=1)
        s = sigma * math.sqrt(1 / ratio**2 - 1)
        rn = r + s * z
        pnl, before, _ = _compound(rn, spec.initial_capital)
        df = _trades_from_daily(dates, pnl, before, prices, spec, f"N{nid}-", every_day)
        _write_csv(df, sens / f"trades_{nid}.csv")
        neighbors.append({"id": nid, "params": {**base_params, **change}})
    grid = {"base": {"id": "base", "params": base_params}, "steps": steps, "neighbors": neighbors}
    (sens / "grid.json").write_text(json.dumps(grid, indent=2))


def _write_paper(dest, spec, rng, end_ts, last_price):
    paper = dest / "paper"
    paper.mkdir()
    p_start = (end_ts.normalize() + pd.Timedelta(days=spec.paper_start_delay_days))
    p_end = p_start + pd.Timedelta(days=spec.paper_days)
    cap = spec.initial_capital
    days = pd.date_range(p_start, p_end - pd.Timedelta(days=1), freq="2D")
    nets = cap * (0.0005 + 0.002 * _standardized(rng, len(days)))
    paper_nets = nets * 0.97 + spec.paper_offset * cap / len(days)
    flat = replace(spec, sides="alternate")
    shadow = _trades_from_daily(days, nets, np.full(len(days), cap), np.full(len(days), last_price), flat, "S",
                                np.ones(len(days), dtype=bool))
    paper_tr = _trades_from_daily(days, paper_nets, np.full(len(days), cap), np.full(len(days), last_price), flat,
                                  "P", np.ones(len(days), dtype=bool))
    _write_csv(shadow, paper / "shadow_trades.csv")
    _write_csv(paper_tr, paper / "paper_trades.csv")
    (paper / "paper_manifest.json").write_text(json.dumps({
        "strategy_version": spec.paper_version or spec.strategy_version,
        "paper_start": _iso(p_start),
        "paper_end": _iso(p_end),
    }, indent=2))


def review_text(version: str, reviewer: str, verdict_line: str) -> str:
    return (
        "# Risk review\n\n"
        f"strategy_version: {version}\n"
        f"reviewer: {reviewer}\n\n"
        "Checked drawdown clustering, cost sensitivity and the paper period.\n"
        "Largest risk: regime change in funding conditions.\n\n"
        f"{verdict_line}\n"
    )


def _write_review(dest, spec, signer):
    text = review_text(spec.strategy_version, spec.reviewer_id,
                       f"VERDICT: {spec.review_verdict} — {spec.review_reason}")
    path = dest / "risk_review.md"
    path.write_text(text)
    if signer is not None:
        signer.sign(path)


# ---------------------------------------------------------------- mutations

def edit_manifest(bundle: Path, fn) -> None:
    p = bundle / "manifest.json"
    m = json.loads(p.read_text())
    fn(m)
    p.write_text(json.dumps(m, indent=2))


def edit_csv(path: Path, fn) -> None:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = fn(df)
    df.to_csv(path, index=False)


# The single-gate canaries. Each must fail exactly the named gate.
SINGLE_GATE = {
    "settings_slippage_below_floor": (1, dict(slippage_bps=2.0)),
    "settings_fees_disabled": (1, dict(venue_fees_enabled=False)),
    "settings_fees_not_charged": (1, dict(realized_fee_bps=0.0)),
    "settings_fee_undercharged": (1, dict(realized_fee_bps=3.0)),
    "sample_too_few_trades": (2, dict(active_every=12, alpha_segments=yearly(18.0, 0.004))),
    "quality_low_profit_factor": (3, dict(wash=("2022-01-01", "2023-12-31", 0.01))),
    "pain_deep_drawdown": (4, dict(alpha_segments=yearly(4.5, 0.004), drawdown=("2022-06-01", 30, -0.0095))),
    "benchmark_loses_to_buy_and_hold": (5, dict(bench_segments=(("2022-01-01", "2023-12-31", 4.0),
                                                                ("2024-01-01", "2024-12-31", 0.02)))),
    "neutral_net_beta": (6, dict(beta=0.3)),
    "neutral_asymmetric_beta": (6, dict(convexity=-0.3)),
    "risk_officer_kill": (7, dict(review_verdict="KILL", review_reason="tail loss in funding squeeze")),
    "stress_costs_double": (8, dict(notional_frac=1.5)),
    "plateau_cliff": (9, dict(neighbor_ratios=(0.92, 0.5, 0.9, 0.86))),
    "recent_decay": (10, dict(alpha_segments=yearly(4.5, 0.002, (2022, 2023)) + yearly(0.0, 0.002, (2024,)))),
    "forward_diverges": (11, dict(paper_offset=0.05)),
    "forward_too_short": (11, dict(paper_days=21)),
    "forward_wrong_version": (11, dict(paper_version="b" * 64)),
    "forward_not_out_of_sample": (11, dict(paper_start_delay_days=-10)),
    "selection_too_many_trials": (12, dict(n_trials=1_000_000, alpha_segments=yearly(2.6, 0.002))),
}
