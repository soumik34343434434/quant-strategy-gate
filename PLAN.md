# Build plan

Each step: write the test → see it fail → implement → see it pass → review against SPEC.md → commit, which is the rollback point.

| # | Step | Tests first | Done when |
|---|---|---|---|
| 1 | Scaffold: git, venv, pinned requirements, pytest.ini, config v1 + registry | — | `pytest` runs (0 tests) |
| 2 | Fixture generator (seeded synthetic bundles + mutations, test SSH key, test config copied from production v1) | — | builds a bundle on disk |
| 3 | Canary suite: clean, 12 single-gate breaks, schema, summary, risk review, config, no-override, deploy, determinism, ordering | all written | all **fail** (nothing implemented) |
| 4 | `hashing.py`, `config.py` | `test_config_integrity.py`, `test_metrics.py` (hand-computed) | those pass |
| 5 | `loader.py` | `test_canary_schema.py` | pass |
| 6 | `metrics.py` | `test_metrics.py` | pass |
| 7 | `review.py`, `gates.py`, `verdict.py`, `runner.py` | clean, single-gate, summary, risk review, ordering, determinism | pass |
| 8 | `gate_check.py` CLI | `test_no_override.py`, CLI exit codes | pass |
| 9 | `new_config_version.py` | config versioning tests | pass |
| 10 | `strategy_hash.py`, `deploy.py`, `deploy.sh` | `test_deploy.py` | pass |
| 11 | `explain.py` (optional advisor) | it can't touch the verdict; it degrades without a key | pass |
| 12 | Ship: CI workflow, CODEOWNERS, adapter template, README | full suite green | — |
| 13 | Review: spec walk-through, final-check questions with evidence, "how could it still blow up" | — | written up |
