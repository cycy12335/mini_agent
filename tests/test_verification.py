import json
from pathlib import Path

import pytest

from mini_agent.actions import ActionError, validate_action
from mini_agent.adapters.mock import MockAdapter, call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.models import ToolError
from mini_agent.patching import PatchManager, digest
from mini_agent.policy import PolicyGuard
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestRunner, TestResult
from mini_agent.verification import VerificationPlanner, RegressionWorkspace, failed_regressions


def put(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def create(path, content):
    return {"path": path, "operation": "create", "original_sha256": None, "content": content}


@pytest.mark.parametrize("selector", ["--help", "../other.py", "/absolute.py", "tests/test_one.py::test_one --help", "tests/test_one.py\n--help", "tests/test_one.py::x::x::x"])
def test_test_runner_rejects_untrusted_selectors(tmp_path, interpreter, selector):
    put(tmp_path, "tests/test_one.py", "def test_one(): pass\n")
    runner = TestRunner(PolicyGuard(tmp_path), interpreter, AgentConfig(), authorized=True)
    with pytest.raises((ToolError, OSError)):
        runner.run(tmp_path / "report.json", targets=[selector])
    assert not (tmp_path / "report.json").exists()


def test_safe_node_selection_executes_only_requested_case(tmp_path, interpreter):
    put(tmp_path, "tests/test_one.py", "def test_pass(): assert True\ndef test_fail(): assert False\n")
    result = TestRunner(PolicyGuard(tmp_path), interpreter, AgentConfig(), authorized=True).run(
        tmp_path / "report.json", targets=["tests/test_one.py::test_pass"])
    assert result.passed and list(result.cases) == ["tests/test_one.py::test_pass"]
    assert result.command[-2:] == ["tests/test_one.py::test_pass", "-q"]


@pytest.mark.parametrize("roots", ["../escape", "/external", "--help"])
def test_configuration_cannot_select_external_paths_or_options(tmp_path, roots):
    put(tmp_path, "pytest.ini", "[pytest]\ntestpaths = " + roots + "\n")
    planner = VerificationPlanner(FileTools(PolicyGuard(tmp_path)), AgentConfig())
    with pytest.raises((ToolError, OSError)):
        planner.roots()


def test_nonstandard_pytest_roots_and_empty_baseline(tmp_path, interpreter):
    root = tmp_path / "project"
    put(root, "spec/test_one.py", "def test_one(): assert True\n")
    put(root, "pytest.ini", "[pytest]\ntestpaths = ./spec/\n")
    files = FileTools(PolicyGuard(root))
    roots, _ = VerificationPlanner(files, files.config).roots()
    assert roots == ["spec"]
    audit = AuditLog(tmp_path / "runs", root)
    class Final:
        def next_action(self, messages, schema): return {"type": "final", "message": "Tests only"}
    controller = AgentController(Final(), ToolRegistry(files), audit, files.config, read_only=False,
                                 test_runner=TestRunner(files.policy, interpreter, files.config, authorized=True))
    controller.run("check")
    assert controller.baseline.passed and controller.baseline.command[-2:] == ["spec", "-q"]


def test_early_stopping_configuration_cannot_claim_complete_execution(tmp_path, interpreter):
    put(tmp_path, "tests/test_one.py", "def test_first(): assert False\ndef test_second(): assert False\n")
    put(tmp_path, "pytest.ini", "[pytest]\naddopts = --maxfail=1\n")
    result = TestRunner(PolicyGuard(tmp_path), interpreter, AgentConfig(), authorized=True).run(tmp_path / "report.json")
    assert result.exit_code == 1 and not result.complete


class RegressionAdapter:
    def __init__(self, *, passing=False, missing=False, mutate=False, source_path="mean.py", test_path="tests/test_regression.py", late_edit=None):
        self.step = 0
        self.passing, self.missing, self.mutate = passing, missing, mutate
        self.source_path, self.test_path, self.late_edit = source_path, test_path, late_edit
    def next_action(self, messages, schemas):
        self.step += 1
        if self.step == 1: return call("read_file", path=self.source_path)
        if self.step == 2:
            data = messages[-1]["result"]["data"]
            module = self.source_path.removesuffix(".py").replace("/", ".")
            test = f"from {module} import mean\ndef test_regression(): assert mean([]) == 0\n"
            if self.passing: test = "def test_regression(): assert True\n"
            if self.missing: test = "def test_regression(): open('missing_resource.bin', 'rb')\n"
            if self.mutate:
                test = f"from pathlib import Path\ndef test_regression():\n    Path('{self.source_path}').write_text('modified = True')\n    assert False\n"
            return call("propose_edits", edits=[{"path": data["path"], "content": "def mean(values):\n    return sum(values) / len(values) if values else 0\n", "original_sha256": data["sha256"]},
                                               create(self.test_path, test)], reason="Guard empty input and add regression",
                        evidence_ids=[data["evidence"][0]["id"]])
        if self.late_edit: self.late_edit()
        return {"type": "final", "message": "Inspect validation evidence and scope"}


def build(root, runs, interpreter, adapter, config=None, approve=True):
    config = config or AgentConfig()
    files = FileTools(PolicyGuard(root), config)
    audit = AuditLog(runs, root)
    return AgentController(adapter, ToolRegistry(files), audit, config, read_only=False,
                           approve=lambda p: approve, test_runner=TestRunner(files.policy, interpreter, config, authorized=True))


def project(root, *, tests=True):
    put(root, "mean.py", "def mean(values): return sum(values) / len(values)\n")
    put(root, "pytest.ini", "[pytest]\npythonpath = .\n")
    (root / "tests").mkdir(exist_ok=True)
    if tests: put(root, "tests/test_existing.py", "from mean import mean\ndef test_empty(): assert mean([]) == 0\n")


def test_pre_repair_failure_then_repair_and_complete_scope(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root)
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter())
    assert agent.run("mean([]) returns 0").status == "verified"
    plan = agent.validation_plans[0]
    assert plan["status"] == "verified"
    assert plan["pre_repair"]["failed_cases"] == ["tests/test_regression.py::test_regression"]
    assert [entry["phase"] for entry in agent.test_history] == ["baseline", "pre_regression", "post_edit"]
    assert agent.test_history[1]["scope"]["workspace"] == "isolated_pre_repair_copy"
    assert agent.test_history[1]["failure_types"]["tests/test_regression.py::test_regression"] == "ZeroDivisionError"
    assert agent.audit.read_json("validation.json")["plans"][0]["status"] == "verified"


def test_empty_project_baseline_is_real_and_new_test_proves_bug(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root, tests=False)
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter())
    assert agent.run("mean([]) returns 0").status == "verified"
    assert agent.baseline.exit_code == 5 and not agent.baseline.cases
    assert agent.validation_plans[0]["pre_repair"]["failed_cases"]


@pytest.mark.parametrize("options, blocker", [({"passing": True}, "REGRESSION_NOT_REPRODUCED"),
                                             ({"missing": True}, "REGRESSION_ENVIRONMENT_INCOMPLETE"),
                                             ({"mutate": True}, "REGRESSION_COPY_MUTATED")])
def test_invalid_reproduction_never_writes_project(tmp_path, interpreter, options, blocker):
    root = tmp_path / "project"
    project(root)
    original = (root / "mean.py").read_bytes()
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter(**options))
    assert agent.run("mean([]) returns 0").status == "failed"
    assert agent.validation_plans[0]["blocker"] == blocker
    assert (root / "mean.py").read_bytes() == original and not (root / "tests/test_regression.py").exists()
    assert not (agent.audit.directory / "manifest.json").exists()


def test_plan_stages_target_affected_then_full_suite(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root)
    put(root, "tests/test_related.py", "from mean import mean\ndef test_related(): assert mean([2, 4]) == 3\n")
    put(root, "tests/test_unrelated.py", "def test_unrelated(): assert True\n")
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter())
    assert agent.run("mean([]) returns 0").status == "verified"
    phases = [record["phase"] for record in agent.test_history]
    assert phases == ["baseline", "pre_regression", "post_target", "post_affected", "post_edit"]
    assert not agent.test_history[2]["scope"]["full"] and agent.test_history[-1]["scope"]["full"]


def test_source_change_after_passing_tests_is_unverified(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root)
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter(late_edit=lambda: (root / "mean.py").write_text("human = 1\n")))
    assert agent.run("mean([]) returns 0").status == "unverified"
    assert agent.validation_plans[0]["status"] == "unverified"


def test_declined_creation_does_not_execute_pre_repair_test(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root)
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter(), approve=False)
    assert agent.run("bug").status == "cancelled"
    assert [item["phase"] for item in agent.test_history] == ["baseline"]
    assert not list(agent.audit.directory.glob("validation-*"))


def test_copy_budget_blocks_before_source_write(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root)
    agent = build(root, tmp_path / "runs", interpreter, RegressionAdapter(), AgentConfig(max_validation_copy_bytes=1))
    agent.run("bug")
    assert agent.validation_plans[0]["blocker"] == "REGRESSION_COPY_LIMIT"
    assert not (agent.audit.directory / "manifest.json").exists()


def test_model_cannot_choose_test_commands_or_targets():
    for arguments in ({"command": "shell"}, {"targets": ["outside"]}, {"python": "other"}):
        with pytest.raises(ActionError): validate_action(call("run_tests", **arguments), AgentConfig())


def test_regression_setup_errors_and_skips_are_not_proof():
    for outcome in ("error", "skipped", "passed"):
        result = TestResult([], exit_code=1, complete=True, cases={"test.py::test_one": outcome})
        with pytest.raises(ToolError): failed_regressions(result, ["test.py::test_one"])


def test_partial_target_failure_stops_broader_checks_and_stays_failed(tmp_path, interpreter):
    root = tmp_path / "project"
    project(root)
    put(root, "tests/test_unrelated.py", "def test_unrelated(): assert True\n")
    class WrongRepair(RegressionAdapter):
        def next_action(self, messages, schemas):
            action = super().next_action(messages, schemas)
            if action.get("name") == "propose_edits":
                action["arguments"]["edits"][0]["content"] = "def mean(values): return 1\n"
            return action
    agent = build(root, tmp_path / "runs", interpreter, WrongRepair())
    assert agent.run("bug").status == "failed"
    assert agent.latest_test.scope["full"] is False
    assert agent.validation_plans[0]["status"] == "failed"
    assert agent.test_history[-1]["phase"] == "post_target"


def test_missing_dependency_blocks_before_model_or_patch(tmp_path, interpreter):
    root = tmp_path / "project"
    put(root, "tests/test_missing.py", "import core5_dependency_that_is_not_installed\ndef test_missing(): pass\n")
    put(root, "pytest.ini", "[pytest]\n")
    class NoModel:
        def next_action(self, *args): pytest.fail("Model called with unusable baseline")
    agent = build(root, tmp_path / "runs", interpreter, NoModel())
    assert agent.run("bug").reason == "test_dependency_unavailable"
    assert not agent.validation_plans and not agent.patches.modified_files()
