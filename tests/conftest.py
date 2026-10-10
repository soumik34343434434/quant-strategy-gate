from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fixtures import builder as B
from tests.fixtures.helpers import ROOT, copy_project


@pytest.fixture(scope="session")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    return {"officer": B.make_signer(d, "officer"), "rogue": B.make_signer(d, "rogue")}


@pytest.fixture(scope="session")
def test_config(tmp_path_factory, keys) -> Path:
    """Production v1 thresholds byte-for-byte, with a test risk-officer key."""
    from gatecheck import config as C

    d = tmp_path_factory.mktemp("config")
    (d / "v1").mkdir()
    shutil.copyfile(ROOT / "config" / "v1" / "gates.json", d / "v1" / "gates.json")
    (d / "v1" / "allowed_signers").write_text(B.allowed_signers_line("risk-officer-1", keys["officer"]))
    C.init_registry(d, date="2026-10-10", reason="test config: production v1 thresholds, test reviewer key")
    return d


@pytest.fixture(scope="session")
def clean_master(tmp_path_factory, keys):
    return B.build(tmp_path_factory.mktemp("clean") / "bundle", B.BundleSpec(), keys["officer"])


@pytest.fixture
def clean(tmp_path, clean_master):
    """A private copy of the clean bundle, safe to mutate."""
    dest = tmp_path / "clean"
    shutil.copytree(clean_master.path, dest)
    return replace(clean_master, path=dest)


@pytest.fixture
def make(tmp_path, keys):
    def _make(name: str = "bundle", signer: str | None = "officer", **overrides):
        spec = replace(B.BundleSpec(), **overrides)
        return B.build(tmp_path / name, spec, keys[signer] if signer else None)
    return _make


@pytest.fixture
def check(test_config):
    from gatecheck.runner import run_check

    def _check(bundle: Path, config_dir: Path | None = None):
        return run_check(Path(bundle), config_dir or test_config)
    return _check


@pytest.fixture
def project(tmp_path, test_config) -> Path:
    return copy_project(tmp_path / "project", test_config)
