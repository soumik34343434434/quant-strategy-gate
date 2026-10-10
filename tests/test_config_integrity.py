"""Frozen, hashed, versioned thresholds. Tampering blocks; changes are logged."""
import json
import shutil

import pytest

from gatecheck import config as C
from tests.fixtures.helpers import detail, failed, reason, status


@pytest.fixture
def cfg(tmp_path, test_config):
    d = tmp_path / "config"
    shutil.copytree(test_config, d)
    return d


def assert_config_block(v):
    assert v.verdict == "BLOCKED"
    assert failed(v) == {0}, detail(v)
    assert "config" in reason(v, 0).lower()
    assert all(status(v, g) == "SKIPPED" for g in range(1, 13))


def test_production_config_verifies():
    from tests.fixtures.helpers import ROOT
    active = C.load_active(ROOT / "config")
    assert active.version == "v1"


def test_tampered_threshold_blocks(cfg, clean, check):
    p = cfg / "v1" / "gates.json"
    data = json.loads(p.read_text())
    data["gates"]["quality"]["min_sharpe_exclusive"] = 0.0
    p.write_text(json.dumps(data, indent=2))
    assert_config_block(check(clean.path, cfg))


def test_whitespace_only_edit_still_blocks(cfg, clean, check):
    p = cfg / "v1" / "gates.json"
    p.write_text(p.read_text() + "\n")
    assert_config_block(check(clean.path, cfg))


def test_tampered_allowed_signers_blocks(cfg, clean, check, keys):
    from tests.fixtures.builder import allowed_signers_line
    p = cfg / "v1" / "allowed_signers"
    p.write_text(p.read_text() + allowed_signers_line("mallory", keys["rogue"]))
    assert_config_block(check(clean.path, cfg))


def test_edited_registry_entry_breaks_chain(cfg, clean, check):
    p = cfg / "registry.json"
    reg = json.loads(p.read_text())
    reg["entries"][0]["reason"] = "rewritten history"
    p.write_text(json.dumps(reg, indent=2))
    assert_config_block(check(clean.path, cfg))


def test_missing_registry_blocks(cfg, clean, check):
    (cfg / "registry.json").unlink()
    assert_config_block(check(clean.path, cfg))


def test_missing_config_dir_blocks(tmp_path, clean, check):
    assert_config_block(check(clean.path, tmp_path / "nope"))


def test_new_version_is_logged_and_used(cfg, clean, check):
    C.create_version(cfg, {"gates.quality.min_sharpe_exclusive": 1.2}, reason="tighten after review",
                     date="2026-10-11")
    reg = json.loads((cfg / "registry.json").read_text())
    assert [e["version"] for e in reg["entries"]] == ["v1", "v2"]
    entry = reg["entries"][1]
    assert entry["date"] == "2026-10-11"
    assert entry["reason"] == "tighten after review"
    assert entry["changes"] == [{"key": "gates.quality.min_sharpe_exclusive", "old": 1.0, "new": 1.2}]
    assert entry["prev_entry_sha256"] == reg["entries"][0]["entry_sha256"]
    v = check(clean.path, cfg)
    assert v.config_version == "v2"
    q = next(g for g in v.gates if g.id == 3)
    assert any(c["threshold"] == 1.2 for c in q.checks)


def test_editing_an_old_version_after_upgrade_still_blocks(cfg, clean, check):
    C.create_version(cfg, {"gates.sample.min_closed_trades": 120}, reason="more data", date="2026-10-11")
    p = cfg / "v1" / "gates.json"
    p.write_text(p.read_text().replace('"min_closed_trades": 100', '"min_closed_trades": 1'))
    assert_config_block(check(clean.path, cfg))


@pytest.mark.parametrize("sets,reason_text", [
    ({"gates.quality.min_sharpe_exclusive": 1.2}, ""),
    ({"gates.quality.min_sharpe_exclusive": 1.2}, "   "),
    ({"gates.quality.min_sharp": 1.2}, "typo in key"),
    ({"gates.quality.min_sharpe_exclusive": "1.2"}, "wrong type"),
    ({"gates.quality.min_sharpe_exclusive": True}, "bool for number"),
    ({}, "no changes"),
])
def test_invalid_version_requests_are_refused(cfg, sets, reason_text):
    with pytest.raises(ValueError):
        C.create_version(cfg, sets, reason=reason_text, date="2026-10-11")
    assert len(json.loads((cfg / "registry.json").read_text())["entries"]) == 1


def test_new_benchmark_can_be_added(cfg, clean, check):
    C.create_version(cfg, {"benchmarks.SOL-USD": {"periods_per_year": 365, "max_gap_days": 3}},
                     reason="add SOL", date="2026-10-11")
    assert check(clean.path, cfg).config_version == "v2"


def test_registry_append_only_rule():
    a = {"version": "v1", "entry_sha256": "1"}
    b = {"version": "v2", "entry_sha256": "2"}
    assert C.is_append_only({"entries": [a]}, {"entries": [a, b]})
    assert C.is_append_only({"entries": [a]}, {"entries": [a]})
    assert not C.is_append_only({"entries": [a, b]}, {"entries": [a]})
    assert not C.is_append_only({"entries": [a]}, {"entries": [dict(a, reason="x"), b]})
