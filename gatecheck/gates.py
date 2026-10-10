"""Layer 3: the gates (SPEC §5). Fixed thresholds from the verified config.

Each gate adds explicit checks of the form "passes when <measured> <op> <threshold>".
A check whose measured value is missing or NaN does not pass. Any exception raised
inside a gate turns that gate into FAIL. Nothing here can turn a FAIL into a PASS.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
import pandas as pd

from . import metrics as M
from . import review
from .errors import GateError, InputError, MetricError
from .hashing import sha256_file
from .loader import Core, parse_ts, read_json, read_trades

PASS, FAIL, NA, SKIPPED = "PASS", "FAIL", "N/A", "SKIPPED"
SUMMARY_KEYS = {"sharpe": "higher", "profit_factor": "higher", "max_drawdown": "lower",
                "total_return": "higher", "net_pnl": "higher", "n_trades": "higher"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _num(x) -> bool:
    return isinstance(x, (int, float, np.floating, np.integer)) and not isinstance(x, (bool, np.bool_))


def _passes(measured, op, threshold) -> bool:
    if op == "==":
        return measured is not None and measured == threshold
    if not _num(measured) or not math.isfinite(float(measured)):
        return False
    m, t = float(measured), float(threshold)
    return {">": m > t, ">=": m >= t, "<": m < t, "<=": m <= t, "abs<=": abs(m) <= t}[op]


@dataclass
class GateResult:
    id: int
    key: str
    name: str
    status: str = FAIL
    checks: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    reason: str = ""

    def check(self, name, measured, op, threshold) -> bool:
        if _num(measured):
            measured = float(measured)
        ok = _passes(measured, op, threshold)
        self.checks.append({"name": name, "measured": measured, "comparator": op, "threshold": threshold,
                            "passed": ok})
        return ok

    def fail(self, problem: str) -> None:
        self.problems.append(problem)

    def finish(self) -> None:
        if self.status in (NA, SKIPPED):
            return
        failed = [c for c in self.checks if not c["passed"]]
        self.status = PASS if self.checks and not failed and not self.problems else FAIL
        if self.status == PASS:
            self.reason = "; ".join(f"{c['name']} {fmt(c['measured'])} {c['comparator']} {fmt(c['threshold'])}"
                                    for c in self.checks)
        else:
            self.reason = "; ".join(self.problems + [
                f"{c['name']} is {fmt(c['measured'])}, needs {c['comparator']} {fmt(c['threshold'])}"
                for c in failed])

    def miss(self) -> float | None:
        """Normalized distance from passing. None means a non-numeric failure (ranks first)."""
        if self.problems:
            return None
        out = 0.0
        for c in self.checks:
            if c["passed"]:
                continue
            m, t = c["measured"], c["threshold"]
            if not (_num(m) and _num(t)) or not math.isfinite(m):
                return None
            gap = abs(abs(m) - t) if c["comparator"] == "abs<=" else abs(m - t)
            out = max(out, gap / abs(t) if t != 0 else gap)
        return out


def fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "true" if v else "false"
    if _num(v):
        return f"{v:.4g}"
    return str(v)


class Context:
    """Recomputes everything from raw trades once; metrics that fail stay as errors."""

    def __init__(self, core: Core, cfg):
        self.core, self.cfg = core, cfg
        self.m = core.manifest
        self.cap = float(self.m["initial_capital"])
        self.t = core.trades
        self.nets = M.net_pnl(self.t)

    def attempt(self, fn):
        try:
            return fn(), None
        except GateError as e:
            return None, str(e)

    @cached_property
    def daily(self):
        return M.daily_pnl(self.t["exit_time"], self.nets, self.core.calendar)

    @cached_property
    def trade_curve(self):
        return self.attempt(lambda: M.returns_from_pnl(self.daily, self.cap))

    @cached_property
    def equity_daily(self):
        return M.equity_on_calendar(self.core.equity, self.core.calendar)

    @cached_property
    def equity_returns(self):
        return self.attempt(lambda: M.returns_from_equity(self.equity_daily, self.cap))

    def return_series(self) -> list[tuple[str, pd.Series]]:
        (tc, terr), (er, eerr) = self.trade_curve, self.equity_returns
        if terr or eerr:
            raise MetricError(terr or eerr)
        return [("trades", tc[0]), ("equity", er)]

    @cached_property
    def sharpe_pair(self):
        return self.attempt(lambda: [M.sharpe(r, self.core.periods_per_year) for _, r in self.return_series()])

    @cached_property
    def drawdown_pair(self):
        def dd():
            tc, err = self.trade_curve
            d_tr = 1.0 if err else M.max_drawdown(tc[1], self.cap)
            return [d_tr, M.max_drawdown(self.equity_daily, self.cap)]
        return self.attempt(dd)

    @cached_property
    def headline(self) -> dict:
        sp, _ = self.sharpe_pair
        dp, _ = self.drawdown_pair
        pf, _ = self.attempt(lambda: M.profit_factor(self.nets))
        bh, _ = self.attempt(self._bh)
        return {
            "sharpe": min(sp) if sp else None,
            "sharpe_trades": sp[0] if sp else None,
            "sharpe_equity": sp[1] if sp else None,
            "profit_factor": pf,
            "max_drawdown": max(dp) if dp else None,
            "max_drawdown_trades": dp[0] if dp else None,
            "max_drawdown_equity": dp[1] if dp else None,
            "total_return": M.total_return(self.nets, self.cap),
            "net_pnl": float(self.nets.sum()),
            "n_trades": int(len(self.t)),
            "buy_and_hold": bh,
            # Informational, not a gate: drawdown and returns only mean anything at this sizing.
            "max_position_to_equity": self.attempt(self._max_position_to_equity)[0],
        }

    def _max_position_to_equity(self) -> float:
        """Largest entry notional divided by the equity at the close before that entry."""
        notional = (self.t["size"] * self.t["entry_price"] * self.m["contract_multiplier"]).to_numpy()
        eq = self.equity_daily
        pos = eq.index.searchsorted(pd.DatetimeIndex(self.t["entry_time"].dt.normalize()), side="left") - 1
        before = np.where(pos >= 0, eq.to_numpy()[np.clip(pos, 0, None)], self.cap)
        return float((notional / before).max())

    def cost_per_side(self) -> float:
        s = self.m["settings"]
        return (s["fee_bps"] + s["slippage_bps"]) / 1e4

    def _bh(self):
        return M.buy_and_hold(self.core.closes, self.cost_per_side())

    def bench_returns(self) -> pd.Series:
        return self.core.closes.pct_change()


# ---------------------------------------------------------------- the gates

def g_integrity(ctx: Context, r: GateResult) -> None:
    r.check("schema and reconciliation of core files", True, "==", True)
    st = ctx.m["strategy_type"]
    if st == "long_only":
        r.check("short trades in a long_only book", int((ctx.t["side"] == "short").sum()), "<=", 0)
    if st == "short_only":
        r.check("long trades in a short_only book", int((ctx.t["side"] == "long").sum()), "<=", 0)
    if "summary" not in ctx.m:
        return
    s = ctx.m["summary"]
    if not isinstance(s, dict):
        r.fail("summary must be an object")
        return
    unknown = sorted(set(s) - set(SUMMARY_KEYS))
    if unknown:
        r.fail(f"summary has unrecognised key(s) {unknown}; allowed: {sorted(SUMMARY_KEYS)}")
    h = ctx.headline
    # Compare against the most favorable recomputation: block only claims nothing in the data supports.
    sharpes = [x for x in (h["sharpe_trades"], h["sharpe_equity"]) if x is not None]
    drawdowns = [x for x in (h["max_drawdown_trades"], h["max_drawdown_equity"]) if x is not None]
    favorable = {
        "sharpe": max(sharpes) if sharpes else None,
        "profit_factor": h["profit_factor"],
        "max_drawdown": min(drawdowns) if drawdowns else None,
        "total_return": h["total_return"], "net_pnl": h["net_pnl"], "n_trades": h["n_trades"],
    }
    tol = ctx.cfg.data["integrity"]
    for key in sorted(set(s) & set(SUMMARY_KEYS)):
        claimed, actual = s[key], favorable[key]
        if not _num(claimed) or not math.isfinite(claimed):
            r.fail(f"summary.{key} must be a finite number")
            continue
        if actual is None:
            r.fail(f"summary claims {key} {claimed:g} but it cannot be recomputed from the trades")
            continue
        slack = tol["summary_sharpe_abs_tol"] if key == "sharpe" else tol["summary_rel_tol"] * max(abs(actual), 1e-9)
        if SUMMARY_KEYS[key] == "higher":
            ok = r.check(f"summary.{key} claimed vs recomputed", claimed, "<=", actual + slack)
        else:
            ok = r.check(f"summary.{key} claimed vs recomputed", claimed, ">=", actual - slack)
        if not ok:
            r.fail(f"summary claims {key} {claimed:g}, trades support {actual:.4g}")


def g_settings(ctx, r):
    g = ctx.cfg.gate("settings")
    s = ctx.m["settings"]
    r.check("venue_fees_enabled", s["venue_fees_enabled"], "==", True)
    r.check("declared fee_bps", s["fee_bps"], ">", 0)
    r.check("declared slippage_bps", s["slippage_bps"], ">=", g["min_slippage_bps"])
    t = ctx.t
    notional = t["size"] * (t["entry_price"] + t["exit_price"]) * ctx.m["contract_multiplier"]
    r.check("share of trades charged fees", float((t["fees"] > 0).mean()), ">=", 1.0)
    r.check("share of trades charged slippage", float((t["slippage"] > 0).mean()), ">=", 1.0)
    frac = g["realized_rate_min_fraction"]
    r.check("min realized fee rate (bps)", float((t["fees"] / notional * 1e4).min()), ">=", frac * s["fee_bps"])
    r.check("min realized slippage rate (bps)", float((t["slippage"] / notional * 1e4).min()), ">=",
            frac * s["slippage_bps"])


def g_sample(ctx, r):
    r.check("closed trades", len(ctx.t), ">=", ctx.cfg.gate("sample")["min_closed_trades"])


def _metric_check(r, name, value_err, op, threshold):
    value, err = value_err
    if err:
        r.fail(f"cannot compute {name}: {err}")
    else:
        r.check(name, value, op, threshold)


def g_quality(ctx, r):
    g = ctx.cfg.gate("quality")
    sp, err = ctx.sharpe_pair
    _metric_check(r, "Sharpe (worse of trades/equity)", (min(sp) if sp else None, err), ">", g["min_sharpe_exclusive"])
    _metric_check(r, "profit factor", ctx.attempt(lambda: M.profit_factor(ctx.nets)), ">",
                  g["min_profit_factor_exclusive"])


def g_pain(ctx, r):
    dp, err = ctx.drawdown_pair
    _metric_check(r, "max drawdown (worse of trades/equity)", (max(dp) if dp else None, err), "<",
                  ctx.cfg.gate("pain")["max_drawdown_exclusive"])


def g_benchmark(ctx, r):
    bh, err = ctx.attempt(ctx._bh)
    if err:
        r.fail(f"cannot compute buy & hold: {err}")
        return
    r.check("total net return vs buy & hold net return", ctx.headline["total_return"], ">", bh)


def g_neutral_book(ctx, r):
    sides = set(ctx.t["side"])
    if sides != {"long", "short"}:
        r.status = NA
        r.reason = f"single-sided book ({'/'.join(sorted(sides))} only): beta gate not applicable"
        return
    g = ctx.cfg.gate("neutral_book")
    b = ctx.bench_returns().iloc[1:]
    worst = {"net": 0.0, "up": 0.0, "down": 0.0}
    n_up, n_down = int((b > 0).sum()), int((b < 0).sum())
    r.check("aligned observations", len(b), ">=", g["min_obs_total"])
    r.check("benchmark-up observations", n_up, ">=", g["min_obs_each_side"])
    r.check("benchmark-down observations", n_down, ">=", g["min_obs_each_side"])
    for _, ret in ctx.return_series():
        y = ret.iloc[1:].to_numpy()
        up, down = M.up_down_beta(y, b.to_numpy())
        for k, v in (("net", M.beta(y, b.to_numpy())), ("up", up), ("down", down)):
            if abs(v) >= abs(worst[k]):
                worst[k] = v
    lim = g["max_abs_beta"]
    r.check("net beta", worst["net"], "abs<=", lim)
    r.check("beta on benchmark-up days", worst["up"], "abs<=", lim)
    r.check("beta on benchmark-down days", worst["down"], "abs<=", lim)


def g_risk_officer(ctx, r):
    verdict, problems = review.evaluate(ctx.core.root, ctx.m, ctx.cfg.allowed_signers,
                                        ctx.cfg.gate("risk_officer")["signature_namespace"])
    for p in problems:
        r.fail(p)
    r.check("risk officer verdict", verdict, "==", "KEEP")


def g_stress(ctx, r):
    g = ctx.cfg.gate("stress")
    stressed = M.net_pnl(ctx.t, g["fee_multiplier"], g["slippage_multiplier"]).sum()
    r.check(f"net return with fees x{g['fee_multiplier']:g} and slippage x{g['slippage_multiplier']:g}",
            stressed / ctx.cap, ">", 0)


def _params_equal(a: dict, b: dict) -> bool:
    return isinstance(a, dict) and isinstance(b, dict) and set(a) == set(b) and all(
        _num(a[k]) and _num(b[k]) and math.isclose(a[k], b[k], rel_tol=0, abs_tol=1e-9) for k in a)


def _trade_sharpe(ctx, trades) -> float:
    daily = M.daily_pnl(trades["exit_time"], M.net_pnl(trades), ctx.core.calendar)
    ret, _ = M.returns_from_pnl(daily, ctx.cap)
    return M.sharpe(ret, ctx.core.periods_per_year)


def g_plateau(ctx, r):
    sens = ctx.core.root / "sensitivity"
    if not sens.is_dir():
        r.fail("sensitivity/ is missing")
        return
    grid = read_json(sens / "grid.json", "sensitivity/grid.json")
    if not isinstance(grid, dict) or not all(k in grid for k in ("base", "steps", "neighbors")):
        raise InputError("sensitivity/grid.json needs base, steps and neighbors")
    base, steps, neighbors = grid["base"], grid["steps"], grid["neighbors"]
    if not (isinstance(base, dict) and isinstance(steps, dict) and isinstance(neighbors, list)):
        raise InputError("sensitivity/grid.json has the wrong shape")
    for n in [base, *neighbors]:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not ID_RE.match(n["id"]):
            raise InputError("sensitivity/grid.json: every entry needs an id of letters, digits, _ or -")
    params = ctx.m["params"]
    if not params:
        r.fail("strategy has no parameters to perturb; plateau cannot be tested")
        return
    if not _params_equal(base.get("params"), params):
        r.fail("sensitivity base params differ from manifest params")
    base_file = sens / f"trades_{base['id']}.csv"
    if not base_file.is_file() or sha256_file(base_file) != sha256_file(ctx.core.root / "trades.csv"):
        r.fail(f"sensitivity/{base_file.name} is not byte-identical to trades.csv")
    required = []
    for p, v in params.items():
        step = steps.get(p)
        if not (_num(step) and step > 0):
            r.fail(f"sensitivity/grid.json has no positive step for parameter {p!r}")
            continue
        for d in (-1, 1):
            want = {**params, p: v + d * step}
            hit = next((n for n in neighbors if _params_equal(n.get("params"), want)), None)
            if hit is None:
                r.fail(f"missing neighbor {p}={v + d * step:g} ({p} {'-' if d < 0 else '+'} one step)")
            else:
                required.append(hit)
    if r.problems:
        return
    base_sharpe = _trade_sharpe(ctx, ctx.t)
    if not base_sharpe > 0:
        r.fail(f"base Sharpe {base_sharpe:.3g} is not positive; plateau ratio undefined")
        return
    window = (ctx.core.start, ctx.core.end)
    ratios = {}
    for n in required:
        trades = read_trades(sens / f"trades_{n['id']}.csv", f"sensitivity/trades_{n['id']}.csv",
                             multiplier=ctx.m["contract_multiplier"],
                             gross_tol=ctx.cfg.data["integrity"]["gross_pnl_rel_tol"], window=window)
        ratios[n["id"]] = _trade_sharpe(ctx, trades) / base_sharpe
    worst = min(ratios, key=ratios.get)
    r.check(f"min neighbor Sharpe / base Sharpe (worst: {worst})", ratios[worst], ">=",
            ctx.cfg.gate("plateau")["min_neighbor_ratio"])


def g_recent(ctx, r):
    gq = ctx.cfg.gate("quality")
    months = ctx.cfg.gate("recent")["trailing_months"]
    w_start = ctx.core.end - pd.DateOffset(months=months)
    cal = ctx.core.calendar[ctx.core.calendar >= w_start.ceil("D")]
    inside = ctx.t["exit_time"] > w_start
    tw, nets_w = ctx.t[inside], ctx.nets[inside]
    eq0 = ctx.cap + float(ctx.nets[~inside].sum())
    if len(cal) < 2 or len(tw) == 0:
        r.fail(f"trailing {months} months has too little data")
        return
    ret_tr, _ = M.returns_from_pnl(M.daily_pnl(tw["exit_time"], nets_w, cal), eq0)
    before = ctx.equity_daily[ctx.equity_daily.index < cal[0]]
    eq_prior = float(before.iloc[-1]) if len(before) else ctx.cap
    ret_eq = M.returns_from_equity(ctx.equity_daily.reindex(cal), eq_prior)
    sh = min(M.sharpe(ret_tr, ctx.core.periods_per_year), M.sharpe(ret_eq, ctx.core.periods_per_year))
    r.check(f"trailing {months}m Sharpe", sh, ">", gq["min_sharpe_exclusive"])
    _metric_check(r, f"trailing {months}m profit factor", ctx.attempt(lambda: M.profit_factor(nets_w)), ">",
                  gq["min_profit_factor_exclusive"])
    bh = M.buy_and_hold(ctx.core.closes.reindex(cal), ctx.cost_per_side())
    r.check(f"trailing {months}m net return vs buy & hold", float(nets_w.sum()) / eq0, ">", bh)


def g_forward(ctx, r):
    g = ctx.cfg.gate("forward")
    paper = ctx.core.root / "paper"
    if not paper.is_dir():
        r.fail("paper/ is missing")
        return
    pm = read_json(paper / "paper_manifest.json", "paper/paper_manifest.json")
    if not isinstance(pm, dict) or not all(k in pm for k in ("strategy_version", "paper_start", "paper_end")):
        raise InputError("paper/paper_manifest.json needs strategy_version, paper_start, paper_end")
    ps = parse_ts(pm["paper_start"], "paper_start")
    pe = parse_ts(pm["paper_end"], "paper_end")
    day = pd.Timedelta(days=1)
    r.check("paper strategy_version matches report", pm["strategy_version"], "==", ctx.m["strategy_version"])
    r.check("days from backtest_end to paper_start", (ps - ctx.core.end) / day, ">=", 0)
    r.check("gap between backtest_end and paper_start (days)", (ps - ctx.core.end) / day, "<=", g["max_gap_days"])
    r.check("paper window (days)", (pe - ps) / day, ">=", g["min_days"])
    kw = dict(multiplier=ctx.m["contract_multiplier"], gross_tol=ctx.cfg.data["integrity"]["gross_pnl_rel_tol"],
              window=(ps, pe), window_name="paper window")
    shadow = read_trades(paper / "shadow_trades.csv", "paper/shadow_trades.csv", **kw)
    traded = read_trades(paper / "paper_trades.csv", "paper/paper_trades.csv", **kw)
    r.check("shadow backtest trades in paper window", len(shadow), ">=", g["min_shadow_trades"])
    diff = abs(M.net_pnl(traded).sum() - M.net_pnl(shadow).sum()) / ctx.cap
    r.check("|paper return - shadow backtest return|", diff, "<=", g["max_abs_return_diff"])
    r.check("|paper trades - shadow trades| / shadow trades", abs(len(traded) - len(shadow)) / len(shadow), "<=",
            g["max_trade_count_diff_rel"])


def g_selection(ctx, r):
    n = ctx.m["n_trials"]
    worst = min(M.deflated_sharpe(ret, n) for _, ret in ctx.return_series())
    r.check(f"deflated Sharpe ratio (n_trials={n})", worst, ">=", ctx.cfg.gate("selection")["min_deflated_sharpe"])


@dataclass(frozen=True)
class Gate:
    id: int
    key: str
    name: str
    fn: object


GATES = (
    Gate(0, "integrity", "Integrity: schema, reconciliation, honest summary", g_integrity),
    Gate(1, "settings", "Settings: venue fees on, slippage at or above floor", g_settings),
    Gate(2, "sample", "Sample: enough closed trades", g_sample),
    Gate(3, "quality", "Quality: Sharpe and profit factor", g_quality),
    Gate(4, "pain", "Pain: max drawdown", g_pain),
    Gate(5, "benchmark", "Benchmark: beats buy & hold net of costs", g_benchmark),
    Gate(6, "neutral_book", "Neutral book: beta near zero, up and down", g_neutral_book),
    Gate(7, "risk_officer", "Risk officer: signed independent KEEP", g_risk_officer),
    Gate(8, "stress", "Stress: profitable with costs doubled", g_stress),
    Gate(9, "plateau", "Plateau: neighbors keep the result", g_plateau),
    Gate(10, "recent", "Recent: trailing window clears quality and benchmark", g_recent),
    Gate(11, "forward", "Forward: paper tracks the backtest", g_forward),
    Gate(12, "selection", "Selection: deflated Sharpe for trials tried", g_selection),
)


def run_gate(gate: Gate, ctx: Context) -> GateResult:
    r = GateResult(gate.id, gate.key, gate.name)
    try:
        gate.fn(ctx, r)
    except GateError as e:
        r.fail(str(e))
    except Exception as e:  # a bug is a failure, never a pass
        r.fail(f"internal error: {type(e).__name__}: {e}")
    r.finish()
    return r


def skipped(gate: Gate, why: str) -> GateResult:
    return GateResult(gate.id, gate.key, gate.name, status=SKIPPED, reason=why)
