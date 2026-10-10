"""Same input, same verdict. Failures ordered by how far they missed."""
import math

from tests.fixtures import builder as B
from tests.fixtures.helpers import detail


def strip_time(d):
    d = dict(d)
    d.pop("checked_at")
    return d


def test_same_input_same_verdict(clean, check):
    a = strip_time(check(clean.path).to_dict())
    b = strip_time(check(clean.path).to_dict())
    assert a == b


def test_any_byte_change_changes_the_input_hash(clean, check):
    h1 = check(clean.path).to_dict()["input"]["bundle_sha256"]
    p = clean.path / "risk_review.md"
    p.write_text(p.read_text() + " ")
    h2 = check(clean.path).to_dict()["input"]["bundle_sha256"]
    assert h1 != h2


def test_failed_gates_ordered_by_miss(make, check):
    built = make(active_every=12, alpha_segments=B.yearly(18.0, 0.004), slippage_bps=2.0)
    (built.path / "risk_review.md").unlink()
    v = check(built.path)
    order = [f["id"] for f in v.to_dict()["failed_gates_by_miss"]]
    assert order[:3] == [7, 1, 2], detail(v)
    misses = [f["miss"] for f in v.to_dict()["failed_gates_by_miss"]]
    numeric = [m for m in misses if m is not None]
    assert numeric == sorted(numeric, reverse=True)
    assert misses[0] is None  # non-numeric failure (missing file) ranks first
    one = next(f for f in v.to_dict()["failed_gates_by_miss"] if f["id"] == 1)
    assert math.isclose(one["miss"], (5.0 - 2.0) / 5.0, rel_tol=1e-9)
