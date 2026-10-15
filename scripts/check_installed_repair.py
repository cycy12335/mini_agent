"""Offline installed-wheel check: regression creation, tests and restart rollback."""

import json
from pathlib import Path
import shutil
import sys
import uuid

import mini_agent
from mini_agent.adapters.mock import call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.patching import PatchManager
from mini_agent.policy import PolicyGuard
from mini_agent.privacy import Redactor, ScopedFiles
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestRunner, validate_interpreter


class RegressionRepair:
    def __init__(self):
        self.step = 0

    def next_action(self, messages, schemas):
        self.step += 1
        data = next((message["result"]["data"] for message in reversed(messages)
                     if message.get("role") == "tool"), {})
        if self.step == 1:
            return call("read_file", path="src/calculator.py")
        if self.step == 2:
            self.source = data
            return call("read_file", path="tests/test_calculator.py")
        if self.step == 3:
            content = self.source["content"].replace("    return sum(values) / len(values)",
                "    if not values:\n        return 0\n    return sum(values) / len(values)")
            return call("propose_edits", edits=[
                {"path": self.source["path"], "original_sha256": self.source["sha256"], "content": content},
                {"path": "tests/test_regression.py", "operation": "create", "original_sha256": None,
                 "content": "from src.calculator import average\ndef test_regression_empty(): assert average([]) == 0\n"}],
                reason="Guard empty input and preserve a regression test",
                evidence_ids=[self.source["evidence"][0]["id"], data["evidence"][0]["id"]])
        if self.step == 4:
            return call("read_file", path="tests/test_regression.py")
        assert data["path"] == "tests/test_regression.py"
        return {"type": "final", "message": "Applied approved guard and regression; see actual tests."}


def main():
    if not Path(mini_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Install the wheel and clear PYTHONPATH first")
    workspace = Path(__file__).resolve().parents[1]
    identifier = uuid.uuid4().hex[:12]
    project = workspace / ".pytest-tmp" / ("installed-core4-" + identifier)
    shutil.copytree(workspace / "examples/buggy_project", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    policy, config = PolicyGuard(project), AgentConfig()
    files = ScopedFiles(policy, config, ["src/calculator.py", "tests/test_calculator.py", "pytest.ini"], Redactor())
    audit = AuditLog(workspace / ".mini-agent/installed-core4" / identifier, project)
    original = (project / "src/calculator.py").read_bytes()
    controller = AgentController(RegressionRepair(), ToolRegistry(files), audit, config, read_only=False,
                                 approve=lambda proposal: True,
                                 test_runner=TestRunner(policy, validate_interpreter(Path(sys.executable)), config, authorized=True))
    result = controller.run("average([]) should return 0")
    assert result.status == "verified", audit.read_json("summary.json")
    assert "tests/test_regression.py" in files.allowed
    assert [entry["exit_code"] for entry in controller.test_history] == [1, 1, 0]
    assert controller.validation_plans[0]["pre_repair"]["failed_cases"]
    restarted = PatchManager(policy, audit.directory, read_only=False)
    restarted.rollback_preview()
    restarted.rollback(True)
    assert not (project / "tests/test_regression.py").exists()
    assert (project / "src/calculator.py").read_bytes() == original
    print(json.dumps({"version": mini_agent.__version__, "installed": mini_agent.__file__,
                      "verification": "repair, regression creation, scope extension, pytest, restart rollback",
                      "artifact": str(audit.directory)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
