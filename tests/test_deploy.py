"""Deploy refuses unless a fresh check of this exact strategy version is DEPLOYABLE."""
import json

import pytest

from tests.fixtures import builder as B
from tests.fixtures.helpers import PY, run


@pytest.fixture
def strategy(tmp_path):
    d = tmp_path / "strategy"
    d.mkdir()
    (d / "strategy.py").write_text("def signal(fast, slow):\n    return fast < slow\n")
    (d / "params.json").write_text(json.dumps({"fast": 10, "slow": 50}))
    return d


def version_of(project, strategy_dir) -> str:
    r = run([PY, project / "strategy_hash.py", strategy_dir])
    assert r.returncode == 0, r.stderr
    v = r.stdout.strip()
    assert len(v) == 64
    return v


def deploy(project, strategy_dir, report):
    return run(["bash", project / "deploy.sh", strategy_dir, report])


def ledger(project):
    p = project / "deployments.json"
    return json.loads(p.read_text())["deployments"] if p.exists() else []


def test_strategy_hash_is_stable_and_content_sensitive(project, strategy):
    a = version_of(project, strategy)
    assert version_of(project, strategy) == a
    (strategy / "params.json").write_text(json.dumps({"fast": 11, "slow": 50}))
    assert version_of(project, strategy) != a


def test_clean_strategy_deploys_with_limits(project, strategy, make):
    built = make(strategy_version=version_of(project, strategy))
    r = deploy(project, strategy, built.path)
    assert r.returncode == 0, r.stdout + r.stderr
    [entry] = ledger(project)
    assert entry["status"] == "live"
    assert entry["strategy_version"] == built.spec.strategy_version
    assert entry["config_version"] == "v1"
    assert entry["limits"]["max_initial_allocation_fraction"] == 0.05
    assert entry["limits"]["kill_switch_drawdown"] == 0.1
    assert "kill switch" in r.stdout.lower()


def test_edited_strategy_is_refused(project, strategy, make):
    built = make(strategy_version=version_of(project, strategy))
    (strategy / "strategy.py").write_text("def signal(fast, slow):\n    return fast <= slow\n")
    r = deploy(project, strategy, built.path)
    assert r.returncode != 0
    assert "version" in (r.stdout + r.stderr).lower()
    assert ledger(project) == []


def test_blocked_report_is_refused_even_with_forged_verdict(project, strategy, make):
    v = version_of(project, strategy)
    built = make(strategy_version=v, review_verdict="KILL")
    forged = project / "verdicts" / v
    forged.mkdir(parents=True)
    (forged / "verdict.json").write_text(json.dumps({"verdict": "DEPLOYABLE", "exit_code": 0,
                                                      "strategy": {"version": v}}))
    r = deploy(project, strategy, built.path)
    assert r.returncode != 0
    assert "BLOCKED" in r.stdout
    assert ledger(project) == []


def test_only_one_live_strategy(project, strategy, make):
    built = make(strategy_version=version_of(project, strategy))
    assert deploy(project, strategy, built.path).returncode == 0
    r = deploy(project, strategy, built.path)
    assert r.returncode != 0
    assert "live" in (r.stdout + r.stderr).lower()
    assert len(ledger(project)) == 1


def test_retire_then_redeploy(project, strategy, make):
    built = make(strategy_version=version_of(project, strategy))
    assert deploy(project, strategy, built.path).returncode == 0
    r = run([PY, project / "deploy.py", "retire", "fixture-meanrev", "--reason", "rolled back"])
    assert r.returncode == 0, r.stderr
    assert ledger(project)[0]["status"] == "retired"
    assert deploy(project, strategy, built.path).returncode == 0


def test_tightened_config_version_applies_to_deploy(project, strategy, make):
    built = make(strategy_version=version_of(project, strategy))
    r = run([PY, project / "new_config_version.py", "--set", "gates.quality.min_sharpe_exclusive=50",
             "--reason", "deliberately impossible for the test"])
    assert r.returncode == 0, r.stderr
    r = deploy(project, strategy, built.path)
    assert r.returncode != 0
    assert ledger(project) == []


@pytest.mark.parametrize("args", [[], ["only-one"], ["a", "b", "c"], ["--force", "a", "b"]])
def test_deploy_rejects_bad_arguments(project, args):
    r = run(["bash", project / "deploy.sh", *args])
    assert r.returncode != 0
