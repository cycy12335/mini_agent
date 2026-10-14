"""Stage 6 cases exercise the guarded controller; scripted models are not live acceptance."""

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from mini_agent.adapters.mock import MockAdapter, call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.policy import PolicyGuard
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestRunner


def build(project, tmp_path, interpreter, adapter, approve=lambda proposal: True):
    policy, config = PolicyGuard(project), AgentConfig()
    return AgentController(adapter, ToolRegistry(FileTools(policy, config)),
                           AuditLog(tmp_path / "runs", project), config, read_only=False, approve=approve,
                           test_runner=TestRunner(policy, interpreter, config, authorized=True))


@pytest.fixture
def case_project(tmp_path, request):
    root = tmp_path / "project"
    fixture = Path(__file__).resolve().parents[1] / "examples" / request.param
    shutil.copytree(fixture, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    return root


class StringRepair:
    def __init__(self):
        self.step = 0

    def next_action(self, messages, schemas):
        self.step += 1
        if self.step == 1:
            return call("read_file", path="src/parser.py")
        if self.step == 2:
            return call("read_file", path="tests/test_parser.py")
        if self.step == 3:
            source = next(m["result"]["data"] for m in messages if m.get("name") == "read_file"
                          and m["result"]["data"]["path"] == "src/parser.py")
            content = source["content"].replace('    return [int(part.strip())',
                                               '    if not text.strip():\n        return []\n    return [int(part.strip())')
            return call("propose_edits", edits=[{"path": source["path"], "original_sha256": source["sha256"], "content": content}])
        return {"type": "final", "message": "Check actual test results"}


@pytest.mark.parametrize("case_project,adapter,expected_failure", [
    ("buggy_project", MockAdapter, "test_average_empty"),
    ("string_input_project", StringRepair, "test_blank_input"),
], indirect=["case_project"])
def test_two_repair_cases_verify_and_restore(case_project, adapter, expected_failure, tmp_path, interpreter):
    snapshot = {p.relative_to(case_project): p.read_bytes() for p in case_project.rglob("*.py")}
    approvals = []
    agent = build(case_project, tmp_path, interpreter, adapter(), approve=lambda p: approvals.append(p) is None)
    result = agent.run("Repair the boundary condition and preserve valid input behavior")
    assert result.status == "verified" and len(approvals) == 1
    summary = agent.audit.read_json("summary.json")
    assert [r["exit_code"] for r in summary["test_history"]] == [1, 0]
    assert len(summary["comparison"]["fixed_failures"]) == 1
    assert summary["comparison"]["fixed_failures"][0].endswith(expected_failure)
    assert not summary["comparison"]["new_failures"]
    assert not summary["comparison"]["missing_cases"]
    agent.patches.rollback_preview()
    agent.patches.rollback(True)
    assert all((case_project / path).read_bytes() == raw for path, raw in snapshot.items())


@pytest.mark.parametrize("case_project", ["missing_dependency_project"], indirect=True)
def test_missing_dependency_stops_before_model_or_write(case_project, tmp_path, interpreter):
    class NoModel:
        def next_action(self, *args):
            pytest.fail("Unusable baseline must stop before model invocation")
    source = case_project / "src/service.py"
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    agent = build(case_project, tmp_path, interpreter, NoModel())
    result = agent.run("Investigate the import failure")
    assert (result.status, result.reason, result.actions) == ("failed", "test_dependency_unavailable", 0)
    assert "selected virtual environment" in result.message and "not installed automatically" in result.message
    summary = agent.audit.read_json("summary.json")
    assert summary["tests"] == "not_passed" and summary["modified_files"] == []
    assert summary["test_exit_code"] == 2 and summary["test_history"][0]["collection_errors"]
    output = (agent.audit.directory / "test-output.txt").read_text(encoding="utf-8")
    assert "ModuleNotFoundError" in output and "mini_agent_fixture_unavailable_dependency_6f4827" in output
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert not (agent.audit.directory / "manifest.json").exists()
