import json
from pathlib import Path
import shutil

import pytest

from mini_agent.cli import main


@pytest.fixture
def demo(tmp_path):
    project = tmp_path / "project"
    source = Path(__file__).resolve().parents[1] / "examples" / "buggy_project"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    return project, tmp_path / "runs"


def run_args(demo):
    project, runs = demo
    return ["run", "--project", str(project), "--task", "average([]) should return 0", "--runs-dir", str(runs)]


@pytest.mark.parametrize("answer", ["", "n", "yes"])
def test_default_decline_preserves_source(demo, monkeypatch, capsys, answer):
    project, runs = demo
    original = (project / "src" / "calculator.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert main(run_args(demo)) == 3
    assert "--- a/src/calculator.py" in capsys.readouterr().out
    assert (project / "src" / "calculator.py").read_bytes() == original
    audit = next(runs.iterdir())
    assert (audit / "proposal-001.diff").exists()
    assert not (audit / "manifest.json").exists()


def test_approved_cli_inspect_and_rollback(demo, monkeypatch, capsys):
    project, runs = demo
    original = (project / "src" / "calculator.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(run_args(demo)) == 2
    assert b"if not values:" in (project / "src" / "calculator.py").read_bytes()
    audit = next(runs.iterdir())
    summary = json.loads((audit / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "unverified" and summary["tests"] == "not_run"
    assert summary["modified_files"] == ["src/calculator.py"]
    capsys.readouterr()
    assert main(["inspect", "--run-id", audit.name, "--runs-dir", str(runs)]) == 0
    assert "unverified" in capsys.readouterr().out
    assert main(["rollback", "--run-id", audit.name, "--runs-dir", str(runs)]) == 0
    assert (project / "src" / "calculator.py").read_bytes() == original
    assert json.loads((audit / "rollback.json").read_text())["status"] == "rolled_back"


def test_readonly_never_prompts_or_writes(demo, monkeypatch):
    project, runs = demo
    original = (project / "src" / "calculator.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Read-only must not prompt"))
    assert main([*run_args(demo), "--read-only"]) == 0
    assert (project / "src" / "calculator.py").read_bytes() == original


def test_ctrl_c_during_approval_is_cancelled(demo, monkeypatch):
    def interrupt(prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    assert main(run_args(demo)) == 3
    assert not list(demo[1].glob("*/manifest.json"))


def test_stored_run_traversal_is_rejected(tmp_path):
    assert main(["inspect", "--run-id", "../outside", "--runs-dir", str(tmp_path)]) == 1
