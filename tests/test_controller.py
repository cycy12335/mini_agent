from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from mini_agent.actions import ActionError
from mini_agent.adapters.mock import MockAdapter, call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.context import ContextLimitError, bounded_context
from mini_agent.controller import AgentController
from mini_agent.policy import PolicyGuard
from mini_agent.tools import FileTools
from mini_agent.tools.registry import ToolRegistry


class ScriptedAdapter:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = []

    def next_action(self, messages, tool_schemas):
        self.messages.append(deepcopy(messages))
        action = next(self.actions)
        if isinstance(action, BaseException):
            raise action
        return action


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "target"
    source = Path(__file__).resolve().parents[1] / "examples" / "buggy_project"
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    return root


def build(project, tmp_path, adapter, config=None, **kwargs):
    config = config or AgentConfig()
    registry = ToolRegistry(FileTools(PolicyGuard(project), config))
    audit = AuditLog(tmp_path / "runs", project)
    return AgentController(adapter, registry, audit, config, **kwargs)


def snapshot(project):
    return {str(p.relative_to(project)): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_end_to_end_mock_proposes_without_writes_or_processes(project, tmp_path, monkeypatch):
    before = snapshot(project)
    def no_process(*args, **kwargs):
        pytest.fail("Agent must not launch a process in stage 2")
    monkeypatch.setattr(subprocess, "Popen", no_process)
    controller = build(project, tmp_path, MockAdapter())
    result = controller.run("average([]) should return 0")
    assert (result.status, result.actions, result.exit_code) == ("proposed", 6, 0)
    assert snapshot(project) == before
    events = [json.loads(line) for line in controller.audit.events.read_text(encoding="utf-8").splitlines()]
    assert [e["action"].get("name", "final") for e in events if e["event"] == "action"] == [
        "list_files", "read_file", "read_file", "search_text", "propose_edits", "final"]
    proposals = json.loads((controller.audit.directory / "proposals.json").read_text(encoding="utf-8"))
    assert "if not values:" in proposals[0][0]["content"]
    summary = json.loads((controller.audit.directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["tests"] == "not_run" and summary["modified_files"] == []
    assert "NOT RUN" in result.report.read_text(encoding="utf-8")


def test_invalid_actions_never_dispatch_and_only_one_correction(project, tmp_path, monkeypatch):
    adapter = ScriptedAdapter([call("shell", command="bad"), "bad json", call("list_files")])
    controller = build(project, tmp_path, adapter)
    monkeypatch.setattr(controller.registry, "execute", lambda action: pytest.fail("Invalid action dispatched"))
    result = controller.run("task")
    assert result.reason == "invalid_action" and result.actions == 2
    assert len(adapter.messages) == 2 and "valid" in adapter.messages[1][-1]["content"]


def test_one_correction_can_recover(project, tmp_path):
    controller = build(project, tmp_path, ScriptedAdapter([
        "bad", call("read_file", path="src/calculator.py"), {"type": "final", "message": "analysis only"}]))
    assert controller.run("task").status == "proposed"


@pytest.mark.parametrize("actions, reason, status", [
    ([call("list_files")] * 3, "action_limit", "failed"),
    ([KeyboardInterrupt()], "keyboard_interrupt", "cancelled"),
    ([RuntimeError("secret must not leak")], "execution_error", "failed"),
])
def test_stop_conditions_have_reports(project, tmp_path, actions, reason, status):
    controller = build(project, tmp_path, ScriptedAdapter(actions), AgentConfig(max_actions=3))
    result = controller.run("task")
    assert result.reason == reason and result.status == status
    assert "secret must not leak" not in result.report.read_text(encoding="utf-8")
    assert json.loads(controller.audit.events.read_text(encoding="utf-8").splitlines()[-1])["event"] == "finished"


def test_cancel_before_dispatch(project, tmp_path):
    flags = iter([False, True])
    controller = build(project, tmp_path, ScriptedAdapter([call("read_file", path="src/calculator.py")]),
                       cancelled=lambda: next(flags))
    assert controller.run("task").status == "cancelled"
    assert not controller.registry.read_files


@pytest.mark.parametrize("clock_values, reason", [([0, 601], "time_limit"),
    ([0, 0, 0, 601], "time_limit"), ([0, 0, 0, 61, 61], "model_timeout")])
def test_time_limits_prevent_dispatch(project, tmp_path, clock_values, reason):
    clock = iter(clock_values)
    controller = build(project, tmp_path, ScriptedAdapter([call("read_file", path="src/calculator.py")]),
                       clock=lambda: next(clock))
    assert controller.run("task").reason == reason
    assert not controller.registry.read_files


def test_context_budget_fails_explicitly(project, tmp_path):
    controller = build(project, tmp_path, MockAdapter(), AgentConfig(max_context_chars=10))
    assert controller.run("task").reason == "context_limit"


def test_context_preserves_task_and_latest_complete_turn():
    messages = [{"role": "system", "task": "keep"}, {"role": "assistant", "action": "x" * 500},
                {"role": "tool", "result": "old"}, {"role": "assistant", "action": "recent"},
                {"role": "tool", "result": "new"}]
    kept = bounded_context(messages, 200)
    assert kept == [messages[0], *messages[-2:]]
    with pytest.raises(ContextLimitError):
        bounded_context(kept, 20)


def test_tool_failure_feedback_and_tests_denied(project, tmp_path):
    adapter = ScriptedAdapter([call("read_file", path="missing.py"), call("run_tests"),
                               {"type": "final", "message": "verified"}])
    result = build(project, tmp_path, adapter).run("task")
    assert result.status == "failed"
    assert adapter.messages[1][-1]["result"]["error_code"] == "NOT_FOUND"
    assert adapter.messages[2][-1]["result"]["error_code"] == "TESTS_NOT_AUTHORIZED"


def test_registry_validates_direct_calls_and_proposals(project, tmp_path):
    registry = build(project, tmp_path, MockAdapter()).registry
    with pytest.raises(ActionError):
        registry.execute(call("run_tests", command="bad"))
    original = registry.files.read_file("src/calculator.py").data
    edit = {"path": "src/calculator.py", "original_sha256": original["sha256"], "content": "pass\n"}
    assert registry.execute(call("propose_edits", edits=[{**edit, "path": "../outside.py"}])).error_code == "INVALID_PATH"
    assert registry.execute(call("propose_edits", edits=[{**edit, "original_sha256": "a" * 64}])).error_code == "HASH_CONFLICT"
    assert registry.execute(call("propose_edits", edits=[edit, edit])).error_code == "DUPLICATE_PATH"
    assert not registry.proposals
    assert registry.execute(call("propose_edits", edits=[{**edit, "path": "pytest.ini"}])).error_code == "EDIT_FORBIDDEN"
    assert registry.execute(call("propose_edits", edits=[edit])).ok
    assert registry.files.read_file("src/calculator.py").data == original


def test_audit_target_overlap_is_rejected_before_writing(project):
    with pytest.raises(ValueError, match="outside"):
        AuditLog(project / "runs", project)
    assert not (project / "runs").exists()


def test_audit_run_ids_are_unique(project, tmp_path):
    first = AuditLog(tmp_path / "runs", project)
    second = AuditLog(tmp_path / "runs", project)
    assert first.run_id != second.run_id


def test_search_records_files_read_even_without_matches(project, tmp_path):
    controller = build(project, tmp_path, ScriptedAdapter([
        call("search_text", query="absent-word"), {"type": "final", "message": "No matches"}]))
    assert controller.run("task").status == "proposed"
    assert "src/calculator.py" in controller.registry.read_files


def test_unrelated_source_is_not_rewritten_by_mock(project, tmp_path):
    (project / "src" / "calculator.py").write_text("def average(values): return 42\n", encoding="utf-8")
    before = snapshot(project)
    controller = build(project, tmp_path, MockAdapter())
    controller.run("task")
    assert not controller.registry.proposals and snapshot(project) == before
