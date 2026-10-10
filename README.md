# Strategy Gate

A deterministic checker that reads a backtest report and says **DEPLOYABLE** (exit 0) or **BLOCKED** (exit 1).

- Every number is recomputed from the raw trades.
- Thresholds live in a frozen, hash-chained config.
- No model, flag or environment variable can change a verdict.

The models advise, the code decides.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m pytest
```

```bash
.venv/bin/python -I gate_check.py path/to/report
```

Output goes to `verdicts/<strategy_version>/verdict.json` and `verdict.md`, plus a line appended to `verdicts/history.jsonl`. Exit codes: 0 DEPLOYABLE, 1 BLOCKED, 2 usage error. There are no options.

**DEPLOYABLE is not permission to scale.** Every verdict also carries:
- the limits: one live strategy, one asset, at most 5% of intended capital, and a kill switch at 10% drawdown that closes everything
- the backtest's largest position as a fraction of capital. Deploy at that sizing, or the drawdown number doesn't apply.

Design documents: [SPEC.md](SPEC.md) (the contract), [ARCHITECTURE.md](ARCHITECTURE.md), [PLAN.md](PLAN.md).

## The report bundle

One directory per strategy version (full field rules are in [SPEC.md §3](SPEC.md)):

```
manifest.json  trades.csv  benchmark.csv  equity.csv
risk_review.md  risk_review.md.sig
sensitivity/grid.json  sensitivity/trades_<id>.csv  (base plus ±1 step for every parameter)
paper/paper_manifest.json  paper/paper_trades.csv  paper/shadow_trades.csv
```

[adapters/rename_columns.py](adapters/rename_columns.py) maps a backtester's trade export onto `trades.csv`. It renames columns and nothing else, so a field it can't map stays missing and the checker blocks. Write an adapter per backtester; the checker trusts none of them.

`strategy_version` is `python strategy_hash.py <strategy_dir>`: a sha256 over every file in the strategy source, params included.

## Research pipeline (BTC-USD)

`research/` turns a strategy idea into a report bundle, using vectorbt and Coinbase daily prices:

```bash
.venv/bin/pip install -r requirements-research.txt
.venv/bin/python research/fetch_btc.py
.venv/bin/python research/run_strategy.py dip_buy
.venv/bin/python -I gate_check.py research/output/dip_buy/<strategy_version>
```

- Strategies live in `research/strategies/<name>/` (`strategy.py` + `params.json`). Their plateau steps are in `research/run_strategy.py`.
- Every parameter set ever run is appended to `research/trials.jsonl`, and its count is the `n_trials` that gate 12 penalizes. Tuning until the gate passes makes gate 12 stricter.
- The pipeline never writes `risk_review.md` or `paper/`. Those must come from someone else and from a real forward period.

| Strategy | Verdict | Biggest misses |
|---|---|---|
| `ma_trend`, SMA 20/100 trend | BLOCKED | 70% drawdown, Sharpe 0.42, 19 trades, trailing 12 months negative |
| `dip_buy`, buy 6%/3-day dips above the 200-day average | BLOCKED | loses to buy & hold, fragile parameters (plateau 19%), 34% drawdown, 75 trades |

## Scorecard (config v1)

| # | Gate | Passes when |
|---|---|---|
| 0 | integrity | Schema and reconciliation pass, and the report's own summary is not more flattering than the trades |
| 1 | settings | Fees on, slippage ≥ 5 bps declared, and every trade actually charged ≥ 95% of the declared rates |
| 2 | sample | ≥ 100 closed trades |
| 3 | quality | Sharpe > 1.0 and profit factor > 1.3 (Sharpe is the worse of the trade-based and equity-curve values) |
| 4 | pain | Max drawdown < 20% (the worse of the two) |
| 5 | benchmark | Net return > buy & hold, net of one round trip of costs |
| 6 | neutral book | For two-sided books: net, up-day and down-day beta all within ±0.2 |
| 7 | risk officer | `VERDICT: KEEP — <reason>` from a reviewer ≠ builder, SSH-signed by a key in the config |
| 8 | stress | Still profitable with fees and slippage doubled |
| 9 | plateau | Every ±1-step neighbor keeps ≥ 70% of the base Sharpe, recomputed from its own trades |
| 10 | recent | The trailing 12 months alone pass gates 3 and 5 |
| 11 | forward | ≥ 4 weeks of paper results after `backtest_end`, within 2 pp of the shadow backtest's return and ±20% of its trade count |
| 12 | selection | Deflated Sharpe ≥ 0.95 given the declared number of variants tried |

Missing, malformed or uncomputable data always blocks. Each gate fails independently, and failed gates are listed in order of how far they missed.

## Risk officer workflow

The builder never signs off their own work. To register a risk officer (a new config version, logged in the registry):

```bash
python new_config_version.py --allowed-signers path/to/allowed_signers --reason "register risk officer jane"
```

`allowed_signers` holds one line per reviewer: `jane namespaces="strategy-gate-review" ssh-ed25519 AAAA...`. Only list risk officers, never builders.

The reviewer writes `risk_review.md`, ending with `VERDICT: KEEP — <biggest reason>` or `VERDICT: KILL — <biggest reason>`, then signs it in their own session with their own key:

```bash
ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n strategy-gate-review risk_review.md
```

Until a reviewer is registered, every report blocks at gate 7. That's intended.

## Changing a threshold

```bash
python new_config_version.py --set gates.quality.min_sharpe_exclusive=1.2 --reason "why"
```

This writes `config/v2/` and appends a hash-chained registry entry with the date, old → new values and the reason.
- Old versions are never edited.
- Editing any config file by hand makes every check block.
- CI rejects a registry that lost or changed history.
- Verdicts record the config version they ran under, and deploy re-checks under the current one.

## Deploy

```bash
bash deploy.sh path/to/strategy_src path/to/report
```

It refuses unless all of these hold:
1. The strategy source hashes to the report's `strategy_version`. Any edit is a new version and needs a new backtest, review and paper period.
2. A fresh, in-process check under the active config is DEPLOYABLE. Stored `verdict.json` files are never trusted.
3. No other strategy is live.

The deploy step itself is a stub: this build holds no keys and places no orders. To retire a live strategy:

```bash
python deploy.py retire <strategy_id> --reason "why"
```

## CI and branch protection

[.github/workflows/strategy-gate.yml](.github/workflows/strategy-gate.yml) runs three checks:
- the full canary suite
- a registry chain and append-only check
- `gate_check` on every changed bundle under `reports/` (all bundles if the config or checker changed)

You must do two things in GitHub, or someone with write access can simply edit the gate:
1. Replace the placeholder owner in [.github/CODEOWNERS](.github/CODEOWNERS).
2. Turn on branch protection for `main`: require code-owner review and require the `strategy-gate` check.

## Optional: plain-English explanation

```bash
pip install -r requirements-explain.txt
```

```bash
python explain.py verdicts/<strategy_version>/verdict.json
```

This sends only the gate table to Claude Opus 5.5 (`claude-opus-5-5`, with server-side refusal fallback enabled) and writes `explanation.md`. It never writes `verdict.json`, nothing reads its output, and it always exits 0. It uses your SDK credentials (`ANTHROPIC_API_KEY` or `ant auth login`), and nothing here asks for a key.

## Final check

| Question | Answer | Evidence |
|---|---|---|
| Does every canary block? | Yes. The clean bundle is DEPLOYABLE, and 19 single-gate breaks block exactly their gate (each of gates 1–12 is covered). | `tests/test_canary_gates.py` |
| Does a missing field block? | Yes. Every trade column, series column, manifest field and settings field is tested, and so is each core file. | `tests/test_canary_schema.py` |
| Does it recompute instead of trusting the summary? | Yes. An inflated Sharpe, PF, drawdown, return, P&L or trade count blocks. A pessimistic summary is ignored. Recomputed metrics match an independent numpy reference. | `tests/test_canary_summary.py`, `tests/test_canary_clean.py` |
| Is there any bypass? | Not in the tool. Extra arguments, `--force`/`--skip`, bypass-sounding environment variables and `PYTHONPATH` injection all fail to unblock. A source scan finds no environment reads. `run_check` takes only (report, config). A tampered config blocks everything. | `tests/test_no_override.py`, `tests/test_config_integrity.py` |
| Does a strategy edit force a fresh check? | Yes. An edited source has a new hash, and deploy refuses. Forged verdicts are ignored because deploy re-checks. | `tests/test_deploy.py` |

Outside the tool, the gate is only as strong as repository permissions: CODEOWNERS, branch protection and required CI. Whoever controls the interpreter controls the process, which is why CI and deploy invoke `python -I`.

## How could this strategy still blow up?

A DEPLOYABLE verdict means the report survived these checks. It does not mean the strategy is safe.

| Risk | Why the gates can't see it | Mitigation |
|---|---|---|
| Look-ahead, survivorship or bad data in the backtester | The checker recomputes the trades but can't know whether they were possible | Gate 11 (out-of-sample paper) is the only defense. Audit the backtester separately. |
| Self-reported inputs | `n_trials`, `builder_id`, the sensitivity trade lists and the paper results are all claims | Keep the full research log. The risk officer should spot-check how the neighbors and paper results were produced. |
| Sizing mismatch | Declaring more capital than you'll trade makes the drawdown look smaller | Deploy at the backtest's `max_position_to_equity`. The verdict prints it. |
| Tail risk: short volatility, negative skew | Sharpe and profit factor look fine until the tail event | The kill switch at 10% drawdown, and small initial size. |
| Leverage and liquidation | The backtest doesn't model margin calls | Account-level leverage cap. Don't deploy leveraged without modeling liquidation. |
| Market impact at size | Slippage is a fixed rate here, but real impact grows with size | Start at ≤ 5% allocation. Re-test slippage before scaling. |
| Funding and borrow costs | Only as good as the `financing` column | The risk officer checks financing against venue history. |
| Regime change and correlation spikes | A neutral book can turn directional in a crash | Gate 6 checks down-day beta. The kill switch closes everything. |
| Paper ≠ live | Paper fills ignore queue position and impact | Compare live to shadow weekly. Re-run the gate if it drifts. |
| Operations | Venue outages, API bugs, fat fingers, venue insolvency | One venue, one asset, small size, and alerting on the kill switch. |

## Layout

```
gate_check.py              CLI (one argument)
gatecheck/                 loader, metrics (layer 2), gates + review + verdict (layer 3), runner, config
config/registry.json       hash-chained config log; config/v1/ thresholds + reviewer keys
deploy.sh, deploy.py       deploy guard and ledger (deployments.json)
new_config_version.py      the only way to change thresholds or reviewers
strategy_hash.py           strategy_version of a source directory
explain.py                 optional Opus 5.5 explanation (advisory)
adapters/                  backtester export -> canonical bundle
ci/                        CI helpers; .github/ workflow + CODEOWNERS
tests/                     canary suite + seeded fixture generator
```
