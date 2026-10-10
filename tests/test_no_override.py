"""A tool that can be talked out of a gate is not a gate."""
import inspect
import re

import pytest

from tests.fixtures import builder as B
from tests.fixtures.helpers import PY, ROOT, clean_env, run


@pytest.fixture
def blocked(make):
    _, overrides = B.SINGLE_GATE["risk_officer_kill"]
    return make("kill", **overrides)


@pytest.mark.parametrize("extra", [
    [], ["--force"], ["--skip", "7"], ["--skip-gates=all"], ["-f"], ["--config", "/tmp"], ["second_report"],
    ["--override"], ["-h"],
])
def test_cli_accepts_exactly_one_positional_argument(extra, blocked, project):
    args = [blocked.path, *extra] if extra != [] else []
    r = run([PY, project / "gate_check.py", *args])
    assert r.returncode != 0
    assert "DEPLOYABLE" not in r.stdout


def test_flag_like_report_path_is_refused(project):
    r = run([PY, project / "gate_check.py", "--force"])
    assert r.returncode != 0


@pytest.mark.parametrize("env", [
    {"FORCE": "1"}, {"SKIP_GATES": "all"}, {"GATE_OVERRIDE": "1"}, {"GATE_CHECK_FORCE": "true"},
    {"DEPLOYABLE": "1"}, {"BYPASS": "yes"}, {"CI": "true", "SKIP": "7"},
])
def test_environment_variables_cannot_unblock(env, blocked, project):
    r = run([PY, project / "gate_check.py", blocked.path], env=clean_env(**env))
    assert r.returncode == 1
    assert "BLOCKED" in r.stdout


def test_pythonpath_module_injection_is_ignored(tmp_path, clean, project):
    evil = tmp_path / "evil"
    (evil / "numpy").mkdir(parents=True)
    marker = tmp_path / "pwned"
    (evil / "numpy" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    r = run([PY, project / "gate_check.py", clean.path],
            env=clean_env(PYTHONPATH=str(evil), PYTHONSTARTUP=str(evil / "numpy" / "__init__.py")))
    assert not marker.exists(), "checker imported code from PYTHONPATH"
    assert r.returncode == 0, r.stdout + r.stderr


def _sources():
    files = [ROOT / "gate_check.py", ROOT / "deploy.py", *sorted((ROOT / "gatecheck").glob("*.py"))]
    assert len(files) > 5
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_environment_reads_in_gate_path(path):
    src = path.read_text()
    assert not re.search(r"\benviron\b|\bgetenv\b|\bputenv\b", src), f"{path.name} reads the environment"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_override_vocabulary_in_gate_path(path):
    src = path.read_text().lower()
    for word in ("--force", "skip_gate", "--skip", "override=", "bypass="):
        assert word not in src, f"{path.name} mentions {word!r}"


def test_gate_check_does_not_parse_options():
    assert "argparse" not in (ROOT / "gate_check.py").read_text()
    assert "argparse" not in "".join(p.read_text() for p in (ROOT / "gatecheck").glob("*.py"))


def test_run_check_signature_has_no_escape_hatch():
    from gatecheck.runner import run_check
    assert list(inspect.signature(run_check).parameters) == ["report_dir", "config_dir"]


def test_explain_is_not_imported_by_the_gate_path():
    for p in _sources():
        assert "explain" not in re.findall(r"^\s*(?:from|import)\s+(\S+)", p.read_text(), re.M), p.name


def test_verdict_requires_every_gate(clean, check):
    """Deleting a gate from the registry of gates cannot produce DEPLOYABLE."""
    from gatecheck import gates
    assert [g.id for g in gates.GATES] == list(range(13))
