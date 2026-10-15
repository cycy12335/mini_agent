import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from mini_agent.cli import main
from mini_agent.config import AgentConfig
from mini_agent.policy import PolicyGuard
from mini_agent.privacy import Redactor, ScopedFiles
from mini_agent.repository import discover_repository
from mini_agent.tools.files import FileTools


def put(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_detects_context_without_reading_or_executing_sources(tmp_path):
    put(tmp_path, "src/package/__init__.py", "raise RuntimeError('do not import')")
    put(tmp_path, "src/package/service.py", "raise RuntimeError('do not import')")
    put(tmp_path, "tests/test_service.py", "def test_service(): pass")
    put(tmp_path, "pyproject.toml", '''[project]
name = "example"
requires-python = ">=3.11"
dependencies = ["requests>=2"]
[project.optional-dependencies]
test = ["pytest"]
[tool.pytest.ini_options]
testpaths = ["tests"]
''')
    put(tmp_path, "requirements-dev.txt", "# comment\npytest>=8\n")
    put(tmp_path, "setup.py", "raise RuntimeError('do not execute')")
    files = FileTools(PolicyGuard(tmp_path))
    overview = discover_repository(files)
    assert overview["language"] == "python"
    assert overview["source_roots"] == ["src"]
    assert {item["module"] for item in overview["modules"]} == {"package", "package.service"}
    assert overview["test_files"] == ["tests/test_service.py"]
    assert overview["configuration"]["pyproject.toml"]["dependencies"] == ["requests>=2"]
    assert overview["configuration"]["requirements-dev.txt"]["requirements"] == ["pytest>=8"]
    assert files.read_paths == {"pyproject.toml", "requirements-dev.txt"}


def test_sensitive_metadata_and_excluded_paths_are_not_disclosed(tmp_path):
    put(tmp_path, "src/service.py", "pass")
    put(tmp_path, "src/secrets.py", "secret = 'private'")
    put(tmp_path, ".hidden/internal.py", "pass")
    put(tmp_path, "pyproject.toml", '[project]\nname="safe"\nsecret="private"\n')
    overview = discover_repository(FileTools(PolicyGuard(tmp_path)), local=False)
    payload = json.dumps(overview)
    assert "private" not in payload and "secrets.py" not in payload and "internal.py" not in payload
    assert "pyproject.toml" not in overview["configuration"]
    assert {"path": "pyproject.toml", "code": "SENSITIVE_CONTENT"} in overview["warnings"]


def test_model_metadata_obeys_explicit_send_scope(tmp_path):
    put(tmp_path, "src/service.py", "pass")
    put(tmp_path, "tests/test_service.py", "pass")
    put(tmp_path, "pyproject.toml", '[project]\nname="not-approved"\n')
    files = ScopedFiles(PolicyGuard(tmp_path), AgentConfig(), ["src/service.py"], Redactor())
    overview = discover_repository(files, local=False)
    assert overview["candidate_files"] == ["src/service.py"]
    assert not overview["configuration"] and not overview["test_files"]
    assert "environments" not in overview and "git" not in overview
    assert not files.read_paths


def test_bad_metadata_is_specific_and_never_echoed(tmp_path):
    put(tmp_path, "pyproject.toml", 'broken="DO_NOT_ECHO')
    overview = discover_repository(FileTools(PolicyGuard(tmp_path)))
    assert {"path": "pyproject.toml", "code": "INVALID_METADATA"} in overview["warnings"]
    assert "DO_NOT_ECHO" not in json.dumps(overview)


def test_custom_setuptools_source_root(tmp_path):
    put(tmp_path, "python_lib/widget/__init__.py", "pass")
    put(tmp_path, "python_lib/widget/service.py", "pass")
    put(tmp_path, "pyproject.toml", '[tool.setuptools.package-dir]\n"" = "python_lib"\n')
    overview = discover_repository(FileTools(PolicyGuard(tmp_path)), local=False)
    assert overview["source_roots"] == ["python_lib"]
    assert {item["module"] for item in overview["modules"]} == {"widget", "widget.service"}


def test_controller_sends_overview_without_counting_it_as_source_evidence(tmp_path):
    from mini_agent.audit import AuditLog
    from mini_agent.controller import AgentController
    from mini_agent.tools.registry import ToolRegistry
    root = tmp_path / "project"
    put(root, "src/service.py", "pass")
    files = FileTools(PolicyGuard(root))
    overview = discover_repository(FileTools(files.policy), local=False)
    class Adapter:
        def next_action(self, messages, tool_schemas):
            assert messages[0]["repository"] == overview
            assert not files.read_paths
            return {"type": "final", "message": "No source evidence inspected"}
    result = AgentController(Adapter(), ToolRegistry(files), AuditLog(tmp_path / "runs", root),
                             AgentConfig(), project_context=overview).run("bug")
    assert result.status == "failed"  # overview alone cannot substantiate a diagnosis


def test_bounded_inventory_and_overview(tmp_path):
    for number in range(50):
        put(tmp_path, f"module_{number}.py", "pass")
    config = AgentConfig(max_files=10, max_tool_output_chars=1000)
    overview = discover_repository(FileTools(PolicyGuard(tmp_path), config), local=False)
    assert overview["truncated"]
    assert len(json.dumps(overview, ensure_ascii=False)) <= 1000


def fake_environment(root, name):
    put(root, f"{name}/pyvenv.cfg", "home = unused")
    return put(root, f"{name}/" + ("Scripts/python.exe" if os.name == "nt" else "bin/python"), "fake")


def test_environment_selected_only_when_unique(tmp_path):
    selected = fake_environment(tmp_path, ".venv")
    overview = discover_repository(FileTools(PolicyGuard(tmp_path)))
    assert overview["selected_interpreter"] == str(selected)
    assert not any(".venv" in path for path in overview["candidate_files"])
    fake_environment(tmp_path, "venv")
    overview = discover_repository(FileTools(PolicyGuard(tmp_path)))
    assert overview["selected_interpreter"] is None
    assert {"code": "AMBIGUOUS_VIRTUAL_ENVIRONMENT"} in overview["warnings"]


def test_project_command_is_offline_readonly(tmp_path, capsys, monkeypatch):
    put(tmp_path, "src/service.py", "pass")
    monkeypatch.setattr("mini_agent.cli.load_api_key", lambda *a: pytest.fail("No credentials needed"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("No approval needed"))
    before = list(tmp_path.rglob("*"))
    assert main(["project", "--project", str(tmp_path)]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert overview["language"] == "python"
    assert list(tmp_path.rglob("*")) == before


def test_cli_auto_selects_environment_without_running_it(tmp_path, monkeypatch):
    root = tmp_path / "project"
    selected = fake_environment(root, ".venv")
    put(root, "src/service.py", "pass")
    captured = {}
    class Controller:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
        def run(self, task):
            from mini_agent.controller import RunResult
            return RunResult("proposed", "test", "test", 0, tmp_path / "report.md")
    monkeypatch.setattr("mini_agent.cli.AgentController", Controller)
    assert main(["run", "--project", str(root), "--task", "bug", "--allow-tests",
                 "--runs-dir", str(tmp_path / "runs")]) == 0
    assert captured["test_runner"].interpreter == selected
    assert captured["project_context"]["modules"][0]["module"] == "service"
    overview_path = next((tmp_path / "runs").glob("*/repository.json"))
    assert json.loads(overview_path.read_text(encoding="utf-8"))["selected_interpreter"] == str(selected)


def test_real_cli_repair_with_auto_detected_environment(project, tmp_path, monkeypatch):
    import sys
    import venv
    environment = project / ".venv"
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
    if sys.prefix != sys.base_prefix:
        site = environment / ("Lib/site-packages" if os.name == "nt" else
                              f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        (site / "test-dependencies.pth").write_text(str(Path(pytest.__file__).parent.parent), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    runs = tmp_path / "runs"
    assert main(["run", "--project", str(project), "--task", "average([]) returns 0",
                 "--allow-tests", "--runs-dir", str(runs)]) == 0
    summary = json.loads(next(runs.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "verified"
    overview = json.loads(next(runs.glob("*/repository.json")).read_text(encoding="utf-8"))
    assert Path(overview["selected_interpreter"]).is_relative_to(environment)


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_git_changes_include_deleted_and_untracked_exclude_sensitive(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init")
    put(tmp_path, "src/changed.py", "original")
    deleted = put(tmp_path, "src/deleted.py", "original")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
    deleted.unlink()
    put(tmp_path, "src/changed.py", "updated")
    put(tmp_path, "extra/new.py", "pass")
    put(tmp_path, "extra/secrets.py", "private")
    overview = discover_repository(FileTools(PolicyGuard(tmp_path)))
    paths = {item["path"] for item in overview["git"]["changes"]}
    assert paths == {"src/changed.py", "src/deleted.py", "extra/new.py"}
