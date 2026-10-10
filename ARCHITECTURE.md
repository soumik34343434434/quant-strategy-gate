# Architecture

```
                 build time                                   run time
  ┌─────────────────────────────┐
  │ Layer 1: Opus 5.5           │  writes code + tests      explain.py ──reads──▶ verdict.json
  │ (advisor, never decides)    │                               │ writes explanation.md only
  └─────────────────────────────┘                               ▼
                                                        (nothing reads it back)

  gate_check.py <report>   ── re-exec under python -I ──▶ gatecheck.runner.run_check()
                                                              │
     ┌───────────────── config.py ◀── config/registry.json ───┤ 1. verify chain + file hashes
     │                                                        │
     │   loader.py   ◀── report bundle (data, never code) ────┤ 2. parse + validate (gate 0)
     │                                                        │
     │   metrics.py  (Layer 2, pure functions)  ◀─────────────┤ 3. recompute every number
     │                                                        │
     └──▶ gates.py + review.py  (Layer 3)  ───────────────────┤ 4. one function per gate
                                                              │
                         verdict.py  ◀────────────────────────┘ 5. verdict.json / verdict.md
                                                                  exit 0 DEPLOYABLE / 1 BLOCKED
```

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `gatecheck/errors.py` | `InputError` (bad data → fail closed), `MetricError` (not computable) | — |
| `gatecheck/hashing.py` | sha256 of files, bundle hash, canonical-JSON hash, source-tree hash | — |
| `gatecheck/config.py` | Load the active config, verify registry chain and file hashes, create new versions | hashing |
| `gatecheck/loader.py` | Strict CSV/JSON parsing to typed frames; manifest validation; trade validation; equity and benchmark checks | errors |
| `gatecheck/metrics.py` | Net P&L, calendars, daily P&L, equity, returns, Sharpe, PF, max DD, B&H, beta, up/down beta, DSR | errors (numpy/pandas/scipy only) |
| `gatecheck/review.py` | Parse the risk review; verify the SSH signature through a fixed `ssh-keygen` argv | errors |
| `gatecheck/gates.py` | One function per gate: (context, config) → `GateResult` | metrics, review, loader |
| `gatecheck/verdict.py` | Assemble verdict, order failures, render JSON and Markdown | — |
| `gatecheck/runner.py` | Orchestrates: config → load → gates → verdict. Catches every exception and turns it into a failing gate. | all of the above |
| `gate_check.py` | CLI: one positional arg, isolated re-exec, writes outputs, exit code | runner |
| `strategy_hash.py` | Deterministic hash of a strategy source dir | hashing |
| `deploy.py` / `deploy.sh` | Hash match → fresh in-process check → ledger | runner, hashing |
| `new_config_version.py` | The only way to change thresholds, reviewers or benchmarks | config |
| `explain.py` | Optional Opus 5.5 explanation | reads verdict.json only |
| `adapters/` | Backtester export → canonical bundle (outside the trust boundary) | — |

## Trust boundaries

1. **Report bundle → loader.** Everything is untrusted data. CSVs are read as strings and converted strictly. Nothing is evaluated or imported, and no text in a report is interpreted.
2. **Config → checker.** Trusted only after the hash chain verifies. The config path is fixed relative to the checker, with no parameter to point it elsewhere. Tests run against a copied project tree with its own config.
3. **Verdict → deploy.** Deploy never reads a stored verdict as authority. It re-runs the check.
4. **Model → anything.** The model output lands in `explanation.md`. No code path reads that file.

## Fail-closed rules in code

- Every gate function runs inside `try/except Exception`. An exception becomes FAIL with reason `internal error: ...`.
- The verdict is DEPLOYABLE only if `all(status in {PASS, N/A})`, with N/A legal only for gate 6. An unknown status counts as a failure.
- Any exception in the CLI exits 1.
- Comparisons involving NaN fail, because `measured > threshold` is False for NaN, and checks are written as "pass when", never "fail when".

## Rollback

- Each module lands as its own git commit on `build/strategy-gate`, so any module can be reverted with `git revert <sha>`.
- Threshold changes are new config versions. To roll a threshold back, create another version whose reason says so; history is never rewritten.
- A deployment is rolled back with `deploy.py retire`.
