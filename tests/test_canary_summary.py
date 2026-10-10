"""The report's own summary is never trusted: flattering claims block."""
import pytest

from tests.fixtures.builder import edit_manifest
from tests.fixtures.helpers import detail, failed, reason, status


@pytest.mark.parametrize("key,delta", [
    ("sharpe", +1.0),
    ("profit_factor", +0.5),
    ("max_drawdown", -0.5),  # applied as a multiplier below: claims half the drawdown
    ("total_return", +0.25),
    ("net_pnl", +10_000.0),
    ("n_trades", +50),
])
def test_flattering_summary_blocks(key, delta, clean, check):
    def inflate(m):
        if key == "max_drawdown":
            m["summary"][key] = m["summary"][key] * 0.5
        else:
            m["summary"][key] = m["summary"][key] + delta
    edit_manifest(clean.path, inflate)
    v = check(clean.path)
    assert v.verdict == "BLOCKED"
    assert failed(v) == {0}, detail(v)
    assert key in reason(v, 0)
    # The data itself is fine, so every other gate is still evaluated.
    assert all(status(v, g) in ("PASS", "N/A") for g in range(1, 13)), detail(v)


def test_summary_claiming_higher_sharpe_than_trades_support(clean, check):
    edit_manifest(clean.path, lambda m: m["summary"].update(sharpe=9.9))
    v = check(clean.path)
    assert failed(v) == {0}, detail(v)
    assert "9.9" in reason(v, 0)


def test_understated_summary_is_ignored_for_gating(clean, check):
    """A pessimistic summary is harmless; gates use recomputed values."""
    edit_manifest(clean.path, lambda m: m["summary"].update(sharpe=0.1))
    v = check(clean.path)
    assert v.verdict == "DEPLOYABLE", detail(v)
    assert v.metrics["sharpe"] > 1.0


def test_no_summary_is_fine(clean, check):
    edit_manifest(clean.path, lambda m: m.pop("summary"))
    assert check(clean.path).verdict == "DEPLOYABLE"


@pytest.mark.parametrize("bad", [{"Sharpe": 1.5}, {"sharpe": "high"}, {"sharpe": True}, "not an object"])
def test_unrecognised_or_malformed_summary_blocks(bad, clean, check):
    edit_manifest(clean.path, lambda m: m.update(summary=bad))
    v = check(clean.path)
    assert failed(v) == {0}, detail(v)
    assert "summary" in reason(v, 0)
