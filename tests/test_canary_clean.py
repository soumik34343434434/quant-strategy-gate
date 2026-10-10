"""The one bundle that must pass, and proof the numbers are recomputed."""
import json

import pytest

from tests.fixtures.helpers import PY, detail, run, status

ALL_GATES = set(range(13))


def test_clean_bundle_is_deployable(clean, check):
    v = check(clean.path)
    assert v.verdict == "DEPLOYABLE", detail(v)
    assert v.exit_code == 0
    assert {g.id for g in v.gates} == ALL_GATES
    assert all(g.status == "PASS" for g in v.gates), detail(v)


def test_recomputed_metrics_match_independent_reference(clean, check):
    v = check(clean.path)
    for key in ("sharpe", "profit_factor", "max_drawdown", "total_return", "net_pnl"):
        assert v.metrics[key] == pytest.approx(clean.expected[key], rel=1e-6), key
    assert v.metrics["n_trades"] == clean.expected["n_trades"]


def test_long_only_book_marks_neutral_gate_not_applicable(make, check):
    v = check(make(strategy_type="long_only", sides="long").path)
    assert status(v, 6) == "N/A", detail(v)
    assert v.verdict == "DEPLOYABLE", detail(v)


def test_cli_exit_zero_and_writes_both_verdict_files(clean, project):
    r = run([PY, project / "gate_check.py", clean.path])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DEPLOYABLE" in r.stdout
    out = project / "verdicts" / clean.spec.strategy_version
    data = json.loads((out / "verdict.json").read_text())
    assert data["verdict"] == "DEPLOYABLE"
    assert data["exit_code"] == 0
    assert data["config"]["version"] == "v1"
    assert len(data["input"]["bundle_sha256"]) == 64
    assert {g["id"] for g in data["gates"]} == ALL_GATES
    for g in data["gates"]:
        for c in g["checks"]:
            assert {"name", "measured", "comparator", "threshold", "passed"} <= set(c)
    md = (out / "verdict.md").read_text()
    assert "DEPLOYABLE" in md and data["input"]["bundle_sha256"] in md
    for g in data["gates"]:
        assert g["name"] in md
    assert "not permission to scale" in md
    history = (project / "verdicts" / "history.jsonl").read_text().strip().splitlines()
    assert json.loads(history[-1])["verdict"] == "DEPLOYABLE"
