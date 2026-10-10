# Strategy Gate — Specification

**Status:** v1, approved 2026-10-10 ("do what is best": recommended defaults accepted).
The review phase checks the build against this document.

## 1. What it is

`python gate_check.py <report_dir>` reads one backtest report bundle and returns **DEPLOYABLE** (exit 0) or **BLOCKED** (exit 1). It writes `verdict.json` and `verdict.md`.
- Every number it acts on is recomputed from raw trades.
- Thresholds come from a frozen, hashed config.
- No model, flag or environment variable can change the result.

Out of scope:
- placing trades, holding exchange keys, network access on the gate path
- detecting bias inside the backtester (§11)

## 2. Three layers that never overlap

| Layer | Component | Does | Never |
|---|---|---|---|
| 1. Advisor | Opus 5.5 (`claude-opus-5-5`) | Build time: writes code and tests, explains failures. Run time (optional): `explain.py` reads a finished `verdict.json` and writes `explanation.md`. | Writes `verdict.json`, affects any exit code, or gets imported by the checker, CI or deploy. |
| 2. Metrics engine | `gatecheck/metrics.py` | Pure functions: validated trades → numbers. | Reads thresholds, files, or the report's summary. |
| 3. Gate checker | `gatecheck/gates.py`, `gatecheck/runner.py`, `gate_check.py` | Config + metrics → per-gate result → verdict + exit code. | Offers any override path. |

## 3. Input: canonical report bundle

The checker reads one format. Adapters (`adapters/`) convert a backtester's export into it. The checker re-validates everything regardless of the adapter.

```
report/
├── manifest.json            required
├── trades.csv               required
├── benchmark.csv            required   timestamp, close
├── equity.csv               required   timestamp, equity  (mark-to-market)
├── risk_review.md           gate 7
├── risk_review.md.sig       gate 7     SSH signature of risk_review.md
├── sensitivity/             gate 9
│   ├── grid.json
│   └── trades_<id>.csv      one trade list per parameter set, incl. base
└── paper/                   gate 11
    ├── paper_manifest.json  strategy_version, paper_start, paper_end
    ├── paper_trades.csv     paper-traded results
    └── shadow_trades.csv    backtest re-run over the paper window
```

**manifest.json**

| Field | Type | Rule |
|---|---|---|
| `strategy_id` | string | non-empty |
| `strategy_version` | string | 64 lowercase hex: `strategy_hash.py` of the strategy source dir (params file included) |
| `builder_id` | string | non-empty |
| `strategy_type` | string | `long_only`, `short_only` or `long_short` |
| `initial_capital` | number | > 0 |
| `currency` | string | non-empty |
| `benchmark_symbol` | string | must be listed in the active config |
| `contract_multiplier` | number | > 0 (1 for spot) |
| `params` | object | numeric values only (needed by gate 9) |
| `n_trials` | integer | ≥ 1: how many variants were tried before this one (gate 12) |
| `backtest_start`, `backtest_end` | ISO-8601 with timezone | start < end |
| `settings` | object | `venue_fees_enabled` (bool), `fee_bps` (number ≥ 0), `slippage_bps` (number ≥ 0), all per side |
| `summary` | object, optional | keys from `sharpe`, `profit_factor`, `max_drawdown`, `total_return`, `net_pnl`, `n_trades`. Unknown keys → BLOCKED. |

**trades.csv** columns: `trade_id, entry_time, exit_time, side, size, entry_price, exit_price, gross_pnl, fees, slippage, financing`. Extra columns are ignored.

### Fail-closed validation (gate 0; any one → BLOCKED with the reason)

- **Files and parsing:**
  - a required file is missing, empty or unparseable, or has no data rows
  - a required column or manifest field is missing
- **Values:**
  - wrong type (a bool is not a number), NaN, ±inf
  - an unparseable timestamp, or one without a timezone
  - `side` is not exactly `long` or `short`
  - `size` ≤ 0, a price ≤ 0, or a negative `fees`, `slippage` or `financing`
- **Trade consistency:**
  - `exit_time < entry_time`, a trade outside `[backtest_start, backtest_end]`, or a duplicate `trade_id`
  - `gross_pnl` ≠ `sign × size × (exit_price − entry_price) × contract_multiplier` beyond tolerance
  - a declared `long_only` with any short trade, or a declared `short_only` with any long trade
- **Benchmark:** doesn't cover the backtest window, or has a gap longer than its configured `max_gap_days`.
- **Equity curve:**
  - timestamps not strictly increasing, or doesn't cover the window
  - first value ≠ `initial_capital`, or last value ≠ `initial_capital + Σ net P&L`, beyond tolerance
- **Summary:** any `summary` metric is more flattering than the most favorable recomputed value beyond tolerance (Sharpe ±0.05 absolute, others ±1% relative).
- **Config:** a config file hash ≠ the registry, or the registry chain is broken. This is checked before the report is opened.

If the core files fail validation, gates 1–12 are reported as `SKIPPED` and the verdict is BLOCKED.

## 4. Metric definitions

- **Net P&L**, per trade: `gross_pnl − fees − slippage − financing`.
- **Calendar:** the benchmark's own daily dates (last close per UTC date) inside the backtest window. `periods_per_year` comes from the config per benchmark: 365 for 24/7 assets, 252 for exchange-hours assets.
- **Trade-based daily P&L:** net P&L booked on the exit date. An exit on a non-calendar day goes to the next calendar date. Days with no exits are 0.
- **Trade-based equity and returns:** equity = `initial_capital + cumsum(daily P&L)`; daily return `r_t = pnl_t / equity_{t−1}`. Equity ≤ 0 at any point → max drawdown = 100% and returns cannot be computed.
- **Equity-based returns:** the equity curve resampled to the calendar (last value per date), then `pct_change`, with `initial_capital` as the first prior value.
- **Sharpe** = `mean(r) / std(r, ddof=1) × √periods_per_year`, with rf = 0. Fewer than 2 observations or zero variance → cannot compute → that gate fails.
- **Profit factor** = Σ winning net P&L / |Σ losing net P&L|. No losing trades → cannot compute → fail.
- **Max drawdown:** the largest fraction below the running peak (the peak includes `initial_capital`).
- **Conservative combination:** Sharpe = min(trade-based, equity-based); max drawdown = max(trade-based, equity-based); |β| = max(trade-based, equity-based).
- **Total return** = Σ net P&L / `initial_capital`.
- **Buy & hold:** `(1 − c) × P_end / P_start × (1 − c) − 1`, with `c = (fee_bps + slippage_bps) / 10⁴`, using the first and last calendar closes.
- **Beta:** OLS slope of strategy daily returns on benchmark daily returns. Up beta and down beta use only benchmark-up days and benchmark-down days respectively.
- **Deflated Sharpe ratio** (Bailey & López de Prado), using per-period Sharpe `SR`, `T` observations, skew `γ₃`, kurtosis `γ₄`:
  - `SR₀ = √(1/(T−1)) · [(1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))]` for N = `n_trials` > 1; `SR₀ = 0` for N = 1
  - `DSR = Φ((SR − SR₀)·√(T−1) / √(1 − γ₃·SR + (γ₄ − 1)/4 · SR²))`
  - The worse of the trade-based and equity-based DSR is used.

## 5. Scorecard (config v1)

| # | Key | Passes when |
|---|---|---|
| 0 | integrity | §3 validation passes |
| 1 | settings | `venue_fees_enabled = true`; `fee_bps > 0`; `slippage_bps ≥ 5`; every trade has fees > 0 and slippage > 0; min realized fee rate ≥ 95% of `fee_bps`; min realized slippage rate ≥ 95% of `slippage_bps`. Realized rate = cost ÷ (entry notional + exit notional). |
| 2 | sample | closed trades ≥ 100 |
| 3 | quality | Sharpe > 1.0 and profit factor > 1.3 |
| 4 | pain | max drawdown < 20% |
| 5 | benchmark | total return > buy & hold return |
| 6 | neutral_book | Applies when the trades contain both sides. \|β\| ≤ 0.2 net, on up days, and on down days, with at least 60 aligned observations in total and 20 per side. N/A when single-sided. |
| 7 | risk_officer | see below |
| 8 | stress | Σ net P&L with fees ×2 and slippage ×2 > 0 |
| 9 | plateau | see below |
| 10 | recent | trades exiting in the 12 months up to `backtest_end` alone pass gate 3 and gate 5 |
| 11 | forward | see below |
| 12 | selection | deflated Sharpe ≥ 0.95 for the declared `n_trials` |

**Gate 7 (risk_officer):**
- `risk_review.md` contains exactly one `strategy_version: <hex>` line, equal to the manifest's.
- It contains exactly one `reviewer: <id>` line, and that id ≠ `builder_id` (case-insensitive).
- Its last non-empty line is `VERDICT: KEEP — <reason>` or `VERDICT: KILL — <reason>` (`—`, `–` or `-`), the reason is non-empty, and no other line starts with `VERDICT`.
- `risk_review.md.sig` verifies with `ssh-keygen -Y verify` against the config's `allowed_signers`, principal = reviewer, namespace `strategy-gate-review`.
- Passes only on KEEP.

**Gate 9 (plateau):**
- `grid.json` lists `base` (`id`, `params` equal to the manifest's), `neighbors` (`id`, `params`), and a numeric `steps` entry for every parameter.
- Every parameter must have both neighbors (`base ± step`, all other parameters unchanged).
- `trades_<base id>.csv` must be byte-identical to `trades.csv`.
- For every neighbor, trade-based Sharpe ≥ 70% of base trade-based Sharpe. The minimum over neighbors is used, and base Sharpe must be > 0.

**Gate 11 (forward):**
- `paper_manifest.strategy_version` equals the manifest's.
- `backtest_end ≤ paper_start ≤ backtest_end + 30 days`.
- `paper_end − paper_start ≥ 28 days`.
- All paper and shadow trades fall inside the paper window.
- At least 5 shadow trades.
- \|paper return − shadow return\| ≤ 2 percentage points (returns relative to `initial_capital`).
- \|paper trade count − shadow trade count\| ≤ 20% of the shadow count.

**Statuses:** each gate is PASS, FAIL, N/A or SKIPPED. A gate fails on its own: a missing `sensitivity/` fails gate 9 only. Any exception inside a gate fails that gate. **DEPLOYABLE** requires every gate to be PASS, or N/A where allowed (gate 6 only).

## 6. Config and no-override

- `config/v<N>/gates.json` and `config/v<N>/allowed_signers` hold every threshold, convention, benchmark and reviewer key.
- `config/registry.json` is append-only and hash-chained. Each entry has `{version, date, files: {name: sha256}, changes: [{key, old, new}], reason, prev_entry_sha256, entry_sha256}`. The last entry is the active version.
- The checker verifies the whole chain and the active files' hashes before reading the report.
- Changing anything means running `new_config_version.py`, which takes the changes and a reason (required), writes `v<N+1>`, and appends a registry entry. Verdicts record the config version they ran under.
- `gate_check.py` takes exactly one positional argument. There are no flags, no environment variables, no `--force` and no skip list. It re-executes itself under `python -I`, so `PYTHONPATH` and similar variables are ignored.
- **Honest limit:** anyone with write access can edit the code. The protection outside the tool is `CODEOWNERS` on the checker and config, plus branch protection with CI required. The owner has to turn those on in GitHub.

## 7. Outputs

`verdicts/<strategy_version>/verdict.json` and `verdict.md` (`verdicts/_invalid/<bundle_sha256>/` when the manifest is unreadable) contain:
- verdict, exit code, `checked_at` (UTC)
- strategy id and version
- bundle sha256 (over sorted `path\0sha256` lines) and per-file hashes
- config version and file hashes, and checker source sha256
- every gate's checks: measured value, comparator, threshold, pass, plus status and reason
- if BLOCKED: failed gates ordered by miss, largest first. Miss = max over failed checks of `|measured − threshold| / max(|threshold|, 1e-9)`; non-numeric failures rank first.
- deployment limits

Determinism: the same bundle with the same config gives identical output apart from `checked_at`.

`verdicts/history.jsonl` is an append-only audit line per run.

`explanation.md` is optional and advisory:
- `explain.py` sends the gate table only (statuses, numbers, thresholds, truncated reasons) to `claude-opus-5-5`.
- It never writes `verdict.json` and is never read by CI or deploy.
- It uses `ANTHROPIC_API_KEY` from the environment; the tool never asks for it.

## 8. Enforcement

**CI** runs:
- pytest, including every canary
- a registry chain check
- a registry append-only check against the base branch
- `gate_check` on every report bundle changed under `reports/`

**`deploy.sh <strategy_dir> <report_dir>`**:
1. `strategy_hash(strategy_dir)` must equal `manifest.strategy_version`.
2. Re-runs the checker in-process under the active config. Stored verdicts are never trusted.
3. Requires DEPLOYABLE.
4. Refuses if any strategy is already live in `deployments.json`.
5. Appends a ledger entry with the limits.

The actual deploy step is a stub. `python deploy.py retire <strategy_id> --reason ...` marks a live strategy retired.

A strategy edit changes its hash. Verdict, risk review and paper run are all bound to the hash, so every edit needs all of them from scratch.

**Deployment limits** (config v1):
- 1 live strategy, 1 asset
- initial allocation ≤ 5% of intended capital
- kill switch at 10% drawdown that closes everything

DEPLOYABLE is not permission to scale.

## 9. Canary tests (written before the checker)

- **Clean bundle** → DEPLOYABLE, exit 0.
- **One mutation per gate 1–12** → BLOCKED, and the failed set is exactly {that gate}.
- **Schema (gate 0):**
  - every required column and manifest field removed
  - wrong type, empty file, header-only file, corrupt CSV, binary garbage
  - bad date, naive date, NaN, inf
  - negative fee, gross P&L mismatch, duplicate trade id
  - trade outside the window, unknown benchmark, declared side mismatch
  - equity not reconciling, benchmark gap, each core file missing
- **Summary:** an inflated Sharpe blocks; an honest summary passes; an unknown key blocks.
- **Risk review:** missing, KILL, no verdict, two verdicts, lowercase, reviewer = builder, wrong hash, unsigned, signed by an unknown key, tampered after signing.
- **Config:** a tampered threshold, a tampered `allowed_signers`, a broken chain, an edited old entry.
- **No override:**
  - extra arguments or flags
  - bypass-sounding environment variables
  - `PYTHONPATH` module injection
  - source scan for environment reads
- **Deploy:**
  - hash mismatch, strategy edit, a hand-edited `verdict.json`, a second live strategy
  - a new config version that tightens a threshold
- **Determinism** and **failure ordering**.

## 10. Final check

Answered with evidence at the end of the build:
1. Does every canary block?
2. Does a missing field block?
3. Does the checker recompute instead of trusting the summary?
4. Is there any bypass?
5. Does a strategy edit force a fresh check?
6. How could this strategy still blow up?

## 11. What the gates cannot see

- **Backtester bias:** look-ahead, survivorship, bad data.
- **Self-declared inputs:** `n_trials` and `builder_id` are taken from the report.
- **Tail risk:** negative skew and short-volatility payoffs.
- **Costs:** market impact at size, and understated financing.
- **Leverage:** liquidation isn't modeled.
- **Regime change:** correlation spikes.
- **Paper vs live:** paper fills are better than live fills.
- **Operations:** outages and bugs.

The README covers each one and the mitigation.

## 12. Resolved decisions

| Question | Decision |
|---|---|
| Build harness | AgenKit not installed. The phases run natively. The tool has no AgenKit runtime dependency. |
| Backtester | Unknown: canonical bundle plus an adapter template. |
| Asset | Benchmarks allow-listed in config: BTC-USD, ETH-USD (365); SPY, QQQ (252). Others need a new config version. |
| Slippage floor | 5 bps per side |
| Paper tolerance | 2 percentage points cumulative return, trade count ±20%, at least 5 shadow trades |
| Gate 6 | Net, up-day and down-day beta, each within ±0.2 |
| Risk review | SSH-signed, verified against the config's allowed signers |
| Equity curve | Mandatory |
| Gate 12 | Deflated Sharpe ≥ 0.95 |
| Deployment limits | 5% of capital, 10% drawdown kill switch |
