"""One broken copy per gate: each must block and name exactly that gate."""
import json
import shutil

import pytest

from tests.fixtures import builder as B
from tests.fixtures.helpers import PY, detail, failed, reason, run


@pytest.mark.parametrize("name", sorted(B.SINGLE_GATE))
def test_single_gate_break_blocks_only_that_gate(name, make, check):
    gate, overrides = B.SINGLE_GATE[name]
    v = check(make(name, **overrides).path)
    assert v.verdict == "BLOCKED"
    assert v.exit_code == 1
    assert failed(v) == {gate}, detail(v)


def test_every_scorecard_gate_has_a_canary():
    assert {g for g, _ in B.SINGLE_GATE.values()} == set(range(1, 13))


@pytest.mark.parametrize("victim,gate", [
    ("risk_review.md", 7),
    ("risk_review.md.sig", 7),
    ("sensitivity", 9),
    ("sensitivity/grid.json", 9),
    ("sensitivity/trades_fast_m.csv", 9),
    ("paper", 11),
    ("paper/paper_manifest.json", 11),
    ("paper/shadow_trades.csv", 11),
    ("paper/paper_trades.csv", 11),
])
def test_missing_gate_input_blocks_only_that_gate(victim, gate, clean, check):
    p = clean.path / victim
    shutil.rmtree(p) if p.is_dir() else p.unlink()
    v = check(clean.path)
    assert v.verdict == "BLOCKED"
    assert failed(v) == {gate}, detail(v)


def test_plateau_base_trades_must_be_the_reported_trades(clean, check):
    p = clean.path / "sensitivity" / "trades_base.csv"
    p.write_text(p.read_text().replace("T00000", "T99999", 1))
    v = check(clean.path)
    assert failed(v) == {9}, detail(v)
    assert "identical" in reason(v, 9)


def test_plateau_requires_both_neighbors_of_every_parameter(clean, check):
    p = clean.path / "sensitivity" / "grid.json"
    grid = json.loads(p.read_text())
    grid["neighbors"] = [n for n in grid["neighbors"] if n["id"] != "slow_p"]
    p.write_text(json.dumps(grid))
    v = check(clean.path)
    assert failed(v) == {9}, detail(v)
    assert "slow" in reason(v, 9)


def test_plateau_base_params_must_match_manifest(clean, check):
    p = clean.path / "sensitivity" / "grid.json"
    grid = json.loads(p.read_text())
    grid["base"]["params"]["fast"] = 11
    p.write_text(json.dumps(grid))
    v = check(clean.path)
    assert failed(v) == {9}, detail(v)


def test_low_sharpe_blocks_on_quality(make, check):
    v = check(make(alpha_segments=B.yearly(0.5, 0.002)).path)
    assert v.verdict == "BLOCKED"
    assert 3 in failed(v), detail(v)


def test_cli_exits_one_on_blocked(make, project):
    gate, overrides = B.SINGLE_GATE["risk_officer_kill"]
    built = make("kill", **overrides)
    r = run([PY, project / "gate_check.py", built.path])
    assert r.returncode == 1
    assert "BLOCKED" in r.stdout
