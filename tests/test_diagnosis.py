from copy import deepcopy
import json

import pytest

from mini_agent.actions import ActionError, validate_action
from mini_agent.adapters.mock import call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.diagnosis import Diagnosis
from mini_agent.policy import PolicyGuard
from mini_agent.privacy import Redactor, ScopedFiles
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestResult, TestRunner


def put(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture
def diagnosis(tmp_path):
    put(tmp_path, "logic.py", "def mean(values):\n    return sum(values) / len(values)\n")
    config = AgentConfig()
    return Diagnosis(FileTools(PolicyGuard(tmp_path), config), config, "Empty values should return 0")


def evidence(diag, revision=0, path="logic.py"):
    result = diag.observe("read_file", diag.files.read_file(path), revision)
    return result.data["evidence"][0]["id"]


def problem(diag):
    return diag.execute("record_problem", {"expected_behavior": "Empty values return 0", "trigger": "mean([])",
                                          "observed_behavior": "Division by zero"}, 0)


def propose(diag, identity):
    result = diag.execute("propose_hypothesis", {"claim": "The denominator is zero for empty input", "evidence_ids": [identity]}, 0)
    assert result.ok
    return result.data["hypothesis"]["id"]


def check(diag, hypothesis, identity, outcome="supported", revision=0):
    return diag.execute("record_check", {"hypothesis_id": hypothesis, "outcome": outcome,
                                         "finding": "Source divides by len(values) without an empty guard", "evidence_ids": [identity]}, revision)


def conclude(diag, hypothesis, revision=0):
    return diag.execute("conclude_root_cause", {"hypothesis_id": hypothesis,
                                               "conclusion": "Empty input creates a zero denominator"}, revision)


def test_structured_source_check_and_root_cause(diagnosis):
    assert problem(diagnosis).ok
    initial = evidence(diagnosis)
    hypothesis = propose(diagnosis, initial)
    assert conclude(diagnosis, hypothesis).error_code == "ROOT_CAUSE_UNSUPPORTED"
    assert check(diagnosis, hypothesis, initial).error_code == "CHECK_EVIDENCE_REQUIRED"
    fresh = evidence(diagnosis)
    assert check(diagnosis, hypothesis, fresh).ok
    root = conclude(diagnosis, hypothesis)
    assert root.ok and root.data["root_cause"]["evidence_ids"] == [initial, fresh]
    summary = diagnosis.summary(0, "model_final")
    assert summary["status"] == "evidence_supported" and not summary["unresolved"]
    assert diagnosis.state["hypotheses"][0]["checks"][0]["finding"]


def test_problem_and_evidence_are_required(diagnosis):
    args = {"claim": "Invented", "evidence_ids": ["E9999"]}
    assert diagnosis.execute("propose_hypothesis", args, 0).error_code == "PROBLEM_REQUIRED"
    problem(diagnosis)
    assert diagnosis.execute("propose_hypothesis", args, 0).error_code == "UNKNOWN_EVIDENCE"
    args["evidence_ids"] = []
    assert diagnosis.execute("propose_hypothesis", args, 0).error_code == "EVIDENCE_REQUIRED"


def test_rejected_and_inconclusive_hypotheses_cannot_be_roots(diagnosis):
    problem(diagnosis)
    hypothesis = propose(diagnosis, evidence(diagnosis))
    assert check(diagnosis, hypothesis, evidence(diagnosis), "rejected").ok
    assert conclude(diagnosis, hypothesis).error_code == "ROOT_CAUSE_UNSUPPORTED"
    result = diagnosis.execute("record_check", {"hypothesis_id": hypothesis, "outcome": "inconclusive",
                                               "finding": "Runtime reproduction not authorized", "evidence_ids": []}, 0)
    assert result.ok
    summary = diagnosis.summary(0, "action_limit")
    assert any(hypothesis in item for item in summary["unresolved"])
    assert summary["termination"] == "action_limit"


def test_stale_source_and_revision_cannot_support_new_checks(diagnosis):
    problem(diagnosis)
    initial = evidence(diagnosis)
    hypothesis = propose(diagnosis, initial)
    fresh = evidence(diagnosis)
    assert check(diagnosis, hypothesis, fresh, revision=1).error_code == "STALE_EVIDENCE"
    (diagnosis.files.policy.root / "logic.py").write_text("def mean(values): return 0\n", encoding="utf-8")
    assert check(diagnosis, hypothesis, fresh).error_code == "STALE_EVIDENCE"
    assert not diagnosis.state["hypotheses"][0]["checks"]


def test_root_invalidates_after_user_edit_but_preserves_pre_patch_history(diagnosis):
    problem(diagnosis)
    hypothesis = propose(diagnosis, evidence(diagnosis))
    check(diagnosis, hypothesis, evidence(diagnosis))
    conclude(diagnosis, hypothesis)
    (diagnosis.files.policy.root / "logic.py").write_text("changed = True\n", encoding="utf-8")
    assert diagnosis.summary(0, "model_final")["status"] == "unconfirmed"
    assert diagnosis.summary(1, "model_final")["status"] == "supported_before_edit"


def test_uncertain_ast_candidates_do_not_support_decisive_check(diagnosis):
    problem(diagnosis)
    initial = evidence(diagnosis)
    hypothesis = propose(diagnosis, initial)
    item = diagnosis.source("logic.py", 1, 2, None, revision=0, tool="find_callers", uncertain=True)
    assert check(diagnosis, hypothesis, item["id"]).error_code == "UNCERTAIN_EVIDENCE"


def test_hypothesis_evidence_and_storage_budgets(diagnosis):
    diagnosis.config = AgentConfig(max_hypotheses=1, max_evidence=2)
    problem(diagnosis)
    initial = evidence(diagnosis)
    hypothesis = propose(diagnosis, initial)
    assert diagnosis.execute("propose_hypothesis", {"claim": "Second", "evidence_ids": [initial]}, 0).error_code == "HYPOTHESIS_LIMIT"
    evidence(diagnosis)
    result = diagnosis.observe("read_file", diagnosis.files.read_file("logic.py"), 0)
    assert not result.data["evidence"] and result.data["evidence_truncated"]
    assert len(diagnosis.state["evidence"]) == 2
    diagnosis.config = AgentConfig(max_diagnosis_chars=100)
    before = deepcopy(diagnosis.state)
    result = diagnosis.execute("record_check", {"hypothesis_id": hypothesis, "outcome": "inconclusive",
                                               "finding": "budget blocked", "evidence_ids": []}, 0)
    assert result.error_code == "DIAGNOSIS_LIMIT" and diagnosis.state == before


def test_forged_search_location_and_sensitive_code_not_evidence(diagnosis):
    result = diagnosis.files.search_text("mean")
    forged = deepcopy(result.data)
    forged["matches"][0]["text"] = "not actual code"
    from mini_agent.models import ToolResult
    observed = diagnosis.observe("search_text", ToolResult(True, forged), 0)
    assert not observed.data["evidence"]
    (diagnosis.files.policy.root / "logic.py").write_text("secret = 'NEVER_STORE'\n", encoding="utf-8")
    observed = diagnosis.observe("read_file", diagnosis.files.read_file("logic.py"), 0)
    assert not observed.data["evidence"] and "NEVER_STORE" not in json.dumps(diagnosis.state)


def test_execution_evidence_is_invalidated_by_user_changes(diagnosis):
    before = diagnosis.snapshot()
    result = TestResult(["python", "-m", "pytest"], exit_code=1, complete=True, cases={"tests/test_mean.py::test_empty": "failed"})
    execution = diagnosis.observe_test(result, "baseline", 0, "test-001.json", before, diagnosis.snapshot())
    problem(diagnosis)
    hypothesis = propose(diagnosis, execution["id"])
    after = diagnosis.observe_test(result, "requested", 0, "test-002.json", before, diagnosis.snapshot())
    assert check(diagnosis, hypothesis, after["id"]).ok
    put(diagnosis.files.policy.root, "new.py", "pass")
    assert conclude(diagnosis, hypothesis).error_code == "STALE_EVIDENCE"


def test_incomplete_execution_is_not_a_supported_check(diagnosis):
    problem(diagnosis)
    hypothesis = propose(diagnosis, evidence(diagnosis))
    snapshot = diagnosis.snapshot()
    result = TestResult(["python"], exit_code=0, complete=False)
    item = diagnosis.observe_test(result, "requested", 0, "test-001.json", snapshot, snapshot)
    assert item["uncertain"]
    assert check(diagnosis, hypothesis, item["id"]).error_code == "UNCERTAIN_EVIDENCE"


@pytest.mark.parametrize("bad", [call("record_check", hypothesis_id="H0001", outcome="verified", finding="x", evidence_ids=[]),
                                call("propose_hypothesis", claim="x", evidence_ids=[{"path": "logic.py"}]),
                                call("propose_hypothesis", claim="x", evidence_ids=["E0001", "E0001"]),
                                call("record_problem", expected_behavior="x", trigger="x", observed_behavior="x", command="shell")])
def test_diagnostic_protocol_rejects_fake_authority(bad):
    with pytest.raises(ActionError):
        validate_action(bad, AgentConfig())


class LocatingAdapter:
    """Protocol integration, deriving all source locations from tool results."""
    def __init__(self, *, stop_after_hypothesis=False):
        self.step = 0
        self.stop_after_hypothesis = stop_after_hypothesis

    def next_action(self, messages, schemas):
        self.step += 1
        latest = next((message["result"]["data"] for message in reversed(messages) if message.get("role") == "tool"), {})
        if self.step == 1:
            return call("record_problem", expected_behavior="checkout([]) returns 0", trigger="Empty input",
                        observed_behavior="ZeroDivisionError")
        if self.step == 2:
            return call("find_definition", symbol="checkout")
        if self.step == 3:
            return call("find_callees", symbol="checkout")
        if self.step == 4:
            self.target = latest["matches"][0]["targets"][0]
            return call("find_definition", symbol=self.target)
        if self.step == 5:
            self.path = latest["matches"][0]["path"]
            return call("propose_hypothesis", claim="Empty input produces a zero denominator", evidence_ids=[latest["evidence"][0]["id"]])
        if self.step == 6:
            self.hypothesis = latest["hypothesis"]["id"]
            return call("read_file", path=self.path)
        if self.step == 7:
            return call("record_check", hypothesis_id=self.hypothesis, outcome="supported",
                        finding="Implementation divides sum by len with no empty-input guard", evidence_ids=[latest["evidence"][0]["id"]])
        if self.step == 8:
            return call("conclude_root_cause", hypothesis_id=self.hypothesis, conclusion="Empty input reaches division by zero")
        return {"type": "final", "message": "Source supports a zero-denominator root cause; runtime unverified."}


@pytest.mark.parametrize("implementation", ["maths.py", "statistics_impl.py"])
def test_cross_file_diagnosis_with_only_task_and_project_path(tmp_path, implementation):
    root = tmp_path / "project"
    put(root, "src/shop/entry.py", f"from .{implementation[:-3]} import mean as evaluate\ndef checkout(values): return evaluate(values)\n")
    put(root, f"src/shop/{implementation}", "def mean(values):\n    return sum(values) / len(values)\n")
    files = FileTools(PolicyGuard(root))
    audit = AuditLog(tmp_path / "runs", root)
    controller = AgentController(LocatingAdapter(), ToolRegistry(files), audit, AgentConfig(), read_only=True)
    result = controller.run("checkout([]) crashes, expected 0")
    assert result.status == "proposed"
    state = audit.read_json("diagnosis.json")
    assert state["summary"]["status"] == "evidence_supported"
    assert state["root_cause"]["hypothesis_id"] == "H0001"
    assert any(item["path"] == f"src/shop/{implementation}" for item in state["evidence"])
    assert "Diagnostic status: evidence_supported" in result.report.read_text(encoding="utf-8")


def test_action_budget_keeps_unconfirmed_hypothesis_and_evidence(tmp_path):
    root = tmp_path / "project"
    put(root, "src/shop/entry.py", "from .maths import mean\ndef checkout(values): return mean(values)\n")
    put(root, "src/shop/maths.py", "def mean(values): return sum(values) / len(values)\n")
    config = AgentConfig(max_actions=5)
    audit = AuditLog(tmp_path / "runs", root)
    controller = AgentController(LocatingAdapter(), ToolRegistry(FileTools(PolicyGuard(root), config)), audit, config)
    result = controller.run("checkout([]) crashes")
    state = audit.read_json("diagnosis.json")
    assert result.reason == "action_limit" and state["hypotheses"][0]["status"] == "pending"
    assert state["summary"]["status"] == "unconfirmed"
    assert any("H0001" in item for item in state["summary"]["unresolved"])
    assert "Unconfirmed:" in result.report.read_text(encoding="utf-8")


def test_model_failure_preserves_diagnostic_progress(tmp_path):
    root = tmp_path / "project"
    put(root, "logic.py", "pass")
    class Failing:
        def next_action(self, messages, schemas):
            if len(messages) == 1:
                return call("record_problem", expected_behavior="zero", trigger="empty", observed_behavior="crash")
            raise RuntimeError("provider failure")
    audit = AuditLog(tmp_path / "runs", root)
    result = AgentController(Failing(), ToolRegistry(FileTools(PolicyGuard(root))), audit, AgentConfig()).run("bug")
    assert result.reason == "execution_error"
    assert audit.read_json("diagnosis.json")["problem"]["trigger"] == "empty"


def test_frozen_scope_blocks_diagnostic_source(tmp_path):
    put(tmp_path, "allowed.py", "pass")
    put(tmp_path, "private.py", "def hidden(): pass")
    files = ScopedFiles(PolicyGuard(tmp_path), AgentConfig(), ["allowed.py"], Redactor())
    diag = Diagnosis(files, files.config, "bug")
    assert diag.source("private.py", 1, 1, None, revision=0, tool="read_file") is None
    assert not diag.state["evidence"]


def test_real_reproduction_supports_diagnosis_without_claiming_repair(project, tmp_path, interpreter):
    class Reproduction:
        def __init__(self):
            self.step = 0
        def next_action(self, messages, schemas):
            self.step += 1
            latest = next(message["result"]["data"] for message in reversed(messages) if message.get("role") == "tool")
            if self.step == 1:
                return call("record_problem", expected_behavior="average([]) returns 0", trigger="Empty input",
                            observed_behavior="ZeroDivisionError")
            if self.step == 2:
                return call("find_definition", symbol="average")
            if self.step == 3:
                return call("propose_hypothesis", claim="Empty input produces a zero denominator",
                            evidence_ids=[latest["evidence"][0]["id"]])
            if self.step == 4:
                self.hypothesis = latest["hypothesis"]["id"]
                return call("run_tests")
            if self.step == 5:
                assert not latest["passed"]
                return call("record_check", hypothesis_id=self.hypothesis, outcome="supported",
                            finding="Authorized reproduction fails on empty input", evidence_ids=[latest["evidence"][0]["id"]])
            if self.step == 6:
                return call("conclude_root_cause", hypothesis_id=self.hypothesis,
                            conclusion="Empty input reaches an unguarded zero denominator")
            return {"type": "final", "message": "Bug reproduced and source cited; no repair applied."}
    policy, config = PolicyGuard(project), AgentConfig()
    audit = AuditLog(tmp_path / "runs", project)
    result = AgentController(Reproduction(), ToolRegistry(FileTools(policy, config)), audit, config,
                             read_only=False, test_runner=TestRunner(policy, interpreter, config, authorized=True)).run("average([]) should return 0")
    assert result.status == "failed"  # tests still fail; diagnostic support is not repair verification
    state = audit.read_json("diagnosis.json")
    assert state["summary"]["status"] == "evidence_supported"
    executions = [item for item in state["evidence"] if item["kind"] == "execution"]
    assert len(executions) == 2
    assert all(item["exit_code"] == 1 and item["failures"] and not item["uncertain"] for item in executions)
    assert not (audit.directory / "manifest.json").exists()
