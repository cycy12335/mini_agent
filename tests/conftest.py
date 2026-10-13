"""Shared isolated project and locally provisioned interpreter for integration tests."""

import os
from pathlib import Path
import shutil
import sys
import venv

import pytest


@pytest.fixture(scope="module")
def interpreter(tmp_path_factory):
    root = tmp_path_factory.mktemp("test-venv")
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(root)
    if sys.prefix != sys.base_prefix:
        site = root / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        (site / "test-dependencies.pth").write_text(str(Path(pytest.__file__).parent.parent), encoding="utf-8")
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "target"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples/buggy_project", root,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    return root
