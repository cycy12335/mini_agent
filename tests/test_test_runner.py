import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import venv

import pytest

from mini_agent.adapters.mock import MockAdapter, call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.models import ToolError
from mini_agent.policy import PolicyGuard
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestRunner, TestResult, compare_results, validate_interpreter


def runner(project, interpreter, **config):
    return TestRunner(PolicyGuard(project), interpreter, AgentConfig(**config), authorized=True)


def replace_test(project, source):
    (project / "tests/test_calculator.py").write_text(source, encoding="utf-8")


def controller(project, tmp_path, interpreter, **options):
    config = AgentConfig()
    policy = PolicyGuard(project)
    return AgentController(options.pop("adapter", MockAdapter()), ToolRegistry(FileTools(policy, config)),
                           AuditLog(tmp_path / "runs", project), config, read_only=options.pop("read_only", False),
                           approve=options.pop("approve", lambda proposal: True),
                           test_runner=TestRunner(policy, interpreter, config, authorized=True), **options)


def test_real_baseline_apply_verify_and_recover(project, tmp_path, interpreter):
    original = (project / "src/calculator.py").read_bytes()
    agent = controller(project, tmp_path, interpreter)
    result = agent.run("average([]) returns zero")
    assert (result.status, result.exit_code) == ("verified", 0)
    summary = agent.audit.read_json("summary.json")
    baseline, latest = summary["test_history"]
    assert baseline["exit_code"] == 1 and latest["exit_code"] == 0
    assert latest["command"] == [str(interpreter), "-m", "pytest", "tests", "-q"]
    assert summary["comparison"]["fixed_failures"] == ["tests/test_calculator.py::test_average_empty"]
    assert not summary["comparison"]["new_failures"]
    assert "2 passed" in (agent.audit.directory / "test-output.txt").read_text(encoding="utf-8")
    agent.patches.rollback_preview()
    agent.patches.rollback(True)
    assert (project / "src/calculator.py").read_bytes() == original


def test_no_authorization_or_readonly_never_spawns(project, tmp_path, interpreter, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("unauthorized process"))
    denied = TestRunner(PolicyGuard(project), interpreter, AgentConfig())
    with pytest.raises(ToolError, match="authorization"):
        denied.run(tmp_path / "report.json")
    agent = controller(project, tmp_path, interpreter, read_only=True)
    assert agent.run("task").status == "proposed"
    assert agent.latest_test is None


def test_environment_is_clean_and_command_is_fixed(project, tmp_path, interpreter, monkeypatch):
    for name in ("MINI_AGENT_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "CUSTOM_SECRET"):
        monkeypatch.setenv(name, "must-not-inherit")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--unknown-argument")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_plugin")
    replace_test(project, '''import os
def test_environment():
    assert not any(k in os.environ for k in ("MINI_AGENT_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "CUSTOM_SECRET"))
    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
''')
    result = runner(project, interpreter).run(tmp_path / "report.json")
    assert result.passed, result.output


def test_output_is_drained_and_bounded(project, tmp_path, interpreter):
    replace_test(project, 'import os\ndef test_flood():\n    os.write(1, b"x" * 1000000)\n    assert False\n')
    result = runner(project, interpreter, max_test_output_bytes=511).run(tmp_path / "report.json")
    assert result.exit_code == 1 and result.complete and not result.passed
    assert result.truncated and len(result.output.encode("utf-8")) <= 511
    assert result.cleanup_ok


@pytest.mark.parametrize("source,exit_code", [
    ('import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n', 0),
    ('# no tests\n', 5),
    ('raise RuntimeError("collection failure")\n', 2),
    ('import os\nos._exit(7)\n', 7),
])
def test_non_verifying_outcomes(project, tmp_path, interpreter, source, exit_code):
    replace_test(project, source)
    result = runner(project, interpreter).run(tmp_path / "report.json")
    assert result.exit_code == exit_code and not result.passed


def test_invalid_interpreter_and_missing_pytest(project, tmp_path):
    with pytest.raises(ToolError):
        validate_interpreter(Path(sys.base_prefix) / "python.exe")
    root = tmp_path / "empty-venv"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = runner(project, python).run(tmp_path / "report.json")
    assert result.exit_code != 0 and not result.complete and not result.passed
    assert "pytest" in result.output


def test_comparison_does_not_count_removed_or_skipped_failures_as_fixed():
    baseline = TestResult([], 1, complete=True, cases={"a": "failed", "b": "passed", "c": "failed"})
    latest = TestResult([], 0, complete=True, cases={"a": "skipped", "d": "passed"})
    comparison = compare_results(baseline, latest)
    assert latest.passed
    assert comparison["fixed_failures"] == []
    assert comparison["unresolved_failures"] == ["a", "c"]
    assert comparison["missing_cases"] == ["b", "c"]
    assert comparison["newly_skipped"] == ["a"]


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("mode", ["timeout", "cancel", "normal"])
def test_real_child_and_grandchild_cleanup(project, tmp_path, interpreter, mode):
    # Grandchild readiness is published before allowing pytest to finish/cancel.
    child = project / "child.py"
    grandchild_code = ("import os,time; from pathlib import Path; "
                       "Path('grandchild.tmp').write_text(str(os.getpid())); "
                       "Path('grandchild.tmp').replace('grandchild.pid'); time.sleep(60)")
    child.write_text("import os,sys,subprocess,time\nfrom pathlib import Path\n"
                     "Path('child.pid').write_text(str(os.getpid()))\n"
                     f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])\n"
                     "time.sleep(60)\n", encoding="utf-8")
    replace_test(project, 'import subprocess,sys,time\nfrom pathlib import Path\n'
                 'def test_tree():\n'
                 '    subprocess.Popen([sys.executable, "child.py"])\n'
                 '    deadline = time.monotonic() + 10\n'
                 '    while not Path("grandchild.pid").exists():\n'
                 '        assert time.monotonic() < deadline\n'
                 '        time.sleep(.01)\n' + ('    time.sleep(60)\n' if mode != "normal" else '    assert True\n'))
    result = runner(project, interpreter, test_timeout_seconds=4 if mode == "timeout" else 20).run(
        tmp_path / "report.json", cancelled=lambda: mode == "cancel" and (project / "grandchild.pid").exists())
    assert result.cleanup_ok
    assert result.timed_out == (mode == "timeout")
    assert result.cancelled == (mode == "cancel")
    assert result.passed == (mode == "normal")
    pids = [int((project / name).read_text()) for name in ("child.pid", "grandchild.pid")]
    deadline = time.monotonic() + 3
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not any(alive(pid) for pid in pids)


def test_failed_tests_allow_second_approved_patch(project, tmp_path, interpreter):
    class RepairAdapter(MockAdapter):
        def next_action(self, messages, schemas):
            if self.step < 4:
                return super().next_action(messages, schemas)
            self.step += 1
            if self.step in {5, 7}:
                data = next(m["result"]["data"] for m in reversed(messages) if m.get("name") == "read_file"
                            and m["result"]["data"]["path"] == "src/calculator.py")
                content = "def average(values):\n    return " + ("1\n" if self.step == 5 else "sum(values) / len(values) if values else 0\n")
                return call("propose_edits", edits=[{"path": data["path"], "original_sha256": data["sha256"], "content": content}])
            if self.step == 6:
                assert not messages[-1]["result"]["data"]["tests"]["data"]["passed"]
                return call("read_file", path="src/calculator.py")
            return {"type": "final", "message": "done"}
    approvals = []
    agent = controller(project, tmp_path, interpreter, adapter=RepairAdapter(),
                       approve=lambda proposal: approvals.append(proposal) is None)
    assert agent.run("task").status == "verified"
    assert len(approvals) == 2
    assert [r["exit_code"] for r in agent.test_history] == [1, 1, 0]
    middle = TestResult(**{k: v for k, v in agent.test_history[1].items() if k not in {"phase", "passed"}})
    assert compare_results(agent.baseline, middle)["new_failures"] == ["tests/test_calculator.py::test_average_non_empty"]


def test_cli_real_authorized_repair(project, tmp_path, interpreter):
    result = subprocess.run([sys.executable, "-m", "mini_agent", "run", "--project", str(project),
                             "--task", "empty average", "--allow-tests", "--test-python", str(interpreter),
                             "--runs-dir", str(tmp_path / "cli-runs")], input="y\n", capture_output=True,
                            encoding="utf-8", timeout=30, env={**os.environ, "PYTHONUTF8": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(next((tmp_path / "cli-runs").glob("*/summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "verified" and summary["tests"] == "passed"


@pytest.mark.parametrize("changes,reason,status", [
    ({"timed_out": True}, "test_timeout", "failed"),
    ({"cancelled": True}, "cancelled", "cancelled"),
    ({"cleanup_ok": False}, "test_execution_error", "failed"),
    ({"complete": False}, "test_execution_error", "failed"),
])
def test_controller_stops_on_unusable_baseline(project, tmp_path, interpreter, monkeypatch, changes, reason, status):
    agent = controller(project, tmp_path, interpreter)
    outcome = TestResult([str(interpreter), "-m", "pytest", "tests", "-q"], exit_code=0,
                         complete=True, cases={"test_ok": "passed"})
    for key, value in changes.items():
        setattr(outcome, key, value)
    monkeypatch.setattr(agent.test_runner, "run", lambda *a, **kw: outcome)
    result = agent.run("task")
    assert (result.status, result.reason) == (status, reason)
    assert not agent.patches.modified_files() and result.actions == 0
    assert agent.audit.read_json("summary.json")["tests"] != "passed"


def test_existing_report_cannot_be_reused(project, tmp_path, interpreter):
    report = tmp_path / "existing.json"
    report.write_text('{}', encoding="utf-8")
    with pytest.raises(ToolError, match="fresh report"):
        runner(project, interpreter).run(report)


def test_pre_cancel_does_not_spawn(project, tmp_path, interpreter, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("spawned after cancellation"))
    result = runner(project, interpreter).run(tmp_path / "report.json", cancelled=lambda: True)
    assert result.cancelled and result.exit_code is None and not result.passed


def test_process_cleanup_error_cannot_verify(project, tmp_path, interpreter, monkeypatch):
    from mini_agent.tools import processes
    tree = processes.WindowsJob if os.name == "nt" else processes.PosixGroup
    original = tree.stop
    def failed_cleanup(self):
        original(self)
        raise OSError("cleanup not confirmed")
    monkeypatch.setattr(tree, "stop", failed_cleanup)
    replace_test(project, 'def test_ok(): assert True\n')
    result = runner(project, interpreter).run(tmp_path / "report.json")
    assert result.exit_code == 0 and not result.cleanup_ok and not result.passed


def test_model_final_cannot_hide_failed_tests(project, tmp_path, interpreter):
    replace_test(project, 'def test_unrelated(): assert False\n')
    agent = controller(project, tmp_path, interpreter)
    result = agent.run("task")
    assert result.status == "failed" and result.reason == "tests_failed"
    assert agent.audit.read_json("summary.json")["tests"] == "not_passed"


def test_model_can_request_only_authorized_fixed_tests(project, tmp_path, interpreter):
    class TestAdapter:
        def __init__(self):
            self.actions = iter([call("run_tests"), call("read_file", path="src/calculator.py"),
                                 {"type": "final", "message": "Tests checked"}])
        def next_action(self, messages, schemas):
            return next(self.actions)
    agent = controller(project, tmp_path, interpreter, adapter=TestAdapter())
    assert agent.run("task").status == "failed"
    assert [item["phase"] for item in agent.test_history] == ["baseline", "requested"]
    assert all(item["command"] == [str(interpreter), "-m", "pytest", "tests", "-q"] for item in agent.test_history)
