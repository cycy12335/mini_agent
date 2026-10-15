from copy import deepcopy
import io
import json

import pytest

from mini_agent.actions import ActionError, validate_action
from mini_agent.adapters.mock import call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.models import ToolError
from mini_agent.panel import PanelInteraction
from mini_agent.patching import PatchManager, digest
from mini_agent.policy import PolicyGuard
from mini_agent.privacy import Redactor, ScopedFiles
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestRunner


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def manager(tmp_path):
    root = tmp_path / "project"
    put(root, "service.py", "def current(): return 1\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return PatchManager(PolicyGuard(root), artifacts, read_only=False)


def create(name="helper.py", content="def helper(): return 2\n"):
    return {"path": name, "operation": "create", "original_sha256": None, "content": content}


def replace(manager, path="service.py", content="def current(): return 2\n"):
    return {"path": path, "original_sha256": digest((manager.policy.root / path).read_bytes()), "content": content}


def test_new_file_apply_journal_restart_and_removal(manager):
    proposal = manager.preview([create()])
    assert "--- /dev/null" in proposal.diff and proposal.edits[0].operation == "create"
    manager.apply(proposal, True)
    assert (manager.policy.root / "helper.py").read_text() == "def helper(): return 2\n"
    entry = manager.manifest["transactions"][0]["files"][0]
    assert entry["original_sha256"] is None and entry["operation"] == "create"
    assert (manager.directory / entry["backup"]).read_bytes() == b""
    restarted = PatchManager(manager.policy, manager.directory, read_only=False)
    assert restarted.rollback_preview() == ["helper.py"]
    restarted.rollback(True)
    assert not (manager.policy.root / "helper.py").exists()
    assert restarted.rollback_preview() == []


def test_create_then_replace_restores_original_absence(manager):
    manager.apply(manager.preview([create()]), True)
    manager.apply(manager.preview([replace(manager, "helper.py", "def helper(): return 3\n")]), True)
    assert manager.rollback_preview() == ["helper.py"]
    manager.rollback(True)
    assert not (manager.policy.root / "helper.py").exists()


def test_creation_collision_after_preview_preserves_user_file(manager):
    proposal = manager.preview([create()])
    put(manager.policy.root, "helper.py", "human = 99\n")
    with pytest.raises(ToolError) as error:
        manager.apply(proposal, True)
    assert error.value.code == "FILE_EXISTS"
    assert (manager.policy.root / "helper.py").read_text() == "human = 99\n"
    assert not manager.manifest_path.exists()


def test_race_at_atomic_creation_does_not_delete_identical_user_content(manager, monkeypatch):
    import mini_agent.patching as patching
    proposal = manager.preview([create()])
    original = patching.os.link
    def collision(source, target):
        target.write_bytes(proposal.edits[0].replacement)
        return original(source, target)
    monkeypatch.setattr(patching.os, "link", collision)
    with pytest.raises(ToolError) as error:
        manager.apply(proposal, True)
    assert error.value.code == "FILE_EXISTS"
    assert (manager.policy.root / "helper.py").read_bytes() == proposal.edits[0].replacement
    assert manager.manifest["transactions"][0]["files"][0]["state"] == "pending"


def test_partial_create_failure_restores_replacement_and_original_absence(manager, monkeypatch):
    import mini_agent.patching as patching
    proposal = manager.preview([replace(manager), create()])
    original = patching.atomic_create
    def interrupt(*args):
        original(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(patching, "atomic_create", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.apply(proposal, True)
    assert (manager.policy.root / "service.py").read_text() == "def current(): return 1\n"
    assert not (manager.policy.root / "helper.py").exists()
    assert manager.manifest["transactions"][0]["status"] == "recovered"


def test_interrupted_creation_journal_can_be_recovered(manager):
    manager.apply(manager.preview([create()]), True)
    manager.manifest["transactions"][0]["status"] = "prepared"
    manager.manifest["transactions"][0]["files"][0]["state"] = "writing"
    manager._save()
    restarted = PatchManager(manager.policy, manager.directory, read_only=False)
    restarted.rollback_preview()
    restarted.rollback(True)
    assert not (manager.policy.root / "helper.py").exists()


def test_human_modified_creation_is_not_deleted(manager):
    manager.apply(manager.preview([create()]), True)
    put(manager.policy.root, "helper.py", "human = 9\n")
    with pytest.raises(ToolError, match="Later changes"):
        manager.rollback_preview()
    assert (manager.policy.root / "helper.py").read_text() == "human = 9\n"


@pytest.mark.parametrize("path", ["../escape.py", ".hidden.py", "secrets.py", "setup.py", "conftest.py", "new.json", "missing/new.py"])
def test_create_policy_blocks_unsafe_targets(manager, path):
    with pytest.raises((ToolError, OSError)):
        manager.preview([create(path)])
    assert not manager.manifest_path.exists()


def test_invalid_python_and_broken_cross_file_export_are_rejected(manager):
    with pytest.raises(ToolError) as error:
        manager.preview([create(content="def broken(:\n")])
    assert error.value.code == "INVALID_PYTHON"
    put(manager.policy.root, "caller.py", "from service import current\ndef run(): return current()\n")
    with pytest.raises(ToolError) as error:
        manager.preview([replace(manager, content="def renamed(): return 2\n")])
    assert error.value.code == "IMPORT_INCONSISTENCY"
    proposal = manager.preview([replace(manager, content="def renamed(): return 2\n"),
                               replace(manager, "caller.py", "from service import renamed\ndef run(): return renamed()\n")])
    assert len(proposal.edits) == 2


def test_new_module_and_importing_replacement_are_checked_together(manager):
    proposal = manager.preview([replace(manager, content="from helper import helper\ndef current(): return helper()\n"), create()])
    manager.apply(proposal, True)
    assert (manager.policy.root / "helper.py").exists()


@pytest.mark.parametrize("change", ["edit", "delete", "new"])
def test_dependency_changes_after_preview_require_new_approval(manager, change):
    path = put(manager.policy.root, "caller.py", "from service import current\n")
    proposal = manager.preview([replace(manager)])
    if change == "edit":
        path.write_text("from service import current\n# user\n", encoding="utf-8")
    elif change == "delete":
        path.unlink()
    else:
        put(manager.policy.root, "new_caller.py", "from service import current\n")
    with pytest.raises(ToolError) as error:
        manager.apply(proposal, True)
    assert error.value.code == "DEPENDENCY_CONFLICT"
    assert (manager.policy.root / "service.py").read_text() == "def current(): return 1\n"


def test_unsaved_editor_changes_block_write_and_rollback(manager):
    dirty = set()
    manager.unsaved_paths = lambda: dirty
    proposal = manager.preview([replace(manager)])
    dirty.add("service.py")
    with pytest.raises(ToolError) as error:
        manager.apply(proposal, True)
    assert error.value.code == "UNSAVED_EDITOR_CONFLICT"
    dirty.clear()
    manager.apply(proposal, True)
    manager.rollback_preview()
    dirty.add("service.py")
    with pytest.raises(ToolError):
        manager.rollback(True)
    assert (manager.policy.root / "service.py").read_text() == "def current(): return 2\n"


def test_test_expected_literal_requires_reason_and_separate_approval(manager):
    put(manager.policy.root, "test_service.py", "from service import current\ndef test_current(): assert current() == 1\n")
    edit = replace(manager, "test_service.py", "from service import current\ndef test_current(): assert current() == 2\n")
    with pytest.raises(ToolError):
        manager.preview([edit])
    proposal = manager.preview([edit], test_change_reason="Requirement now expects 2")
    assert proposal.expectation_changes == ("test_service.py",)
    with pytest.raises(ToolError) as error:
        manager.apply(proposal, True)
    assert error.value.code == "TEST_APPROVAL_REQUIRED"
    manager.apply(proposal, True, expectation_approval=True)
    assert "== 2" in (manager.policy.root / "test_service.py").read_text()


@pytest.mark.parametrize("content", ["def test_current(): assert True\n", "def test_current(): pass\n", "def test_current(): assert 2 == 2\n"])
def test_expectation_reason_does_not_allow_weakened_or_removed_assertions(manager, content):
    put(manager.policy.root, "test_service.py", "def test_current(): assert current() == 1\n")
    with pytest.raises(ToolError):
        manager.preview([replace(manager, "test_service.py", content)], test_change_reason="Attempt to bypass protection")


@pytest.mark.parametrize("edit", [create(), {**create(), "operation": []}, {**create(), "original_sha256": "a" * 64},
                                 {"path": "helper.py", "operation": "replace", "content": "pass", "original_sha256": None}])
def test_create_action_contract(edit):
    action = call("propose_edits", edits=[edit], reason="Extract helper", evidence_ids=["E0001"])
    if edit == create():
        assert validate_action(action, AgentConfig()) == action
    else:
        with pytest.raises(ActionError):
            validate_action(action, AgentConfig())


class RegressionRepair:
    def __init__(self):
        self.step = 0
    def next_action(self, messages, schemas):
        self.step += 1
        data = next((message["result"]["data"] for message in reversed(messages) if message.get("role") == "tool"), {})
        if self.step == 1:
            return call("read_file", path="src/calculator.py")
        if self.step == 2:
            self.source = data
            return call("read_file", path="tests/test_calculator.py")
        if self.step == 3:
            content = self.source["content"].replace("    return sum(values) / len(values)", "    if not values:\n        return 0\n    return sum(values) / len(values)")
            return call("propose_edits", edits=[{"path": self.source["path"], "original_sha256": self.source["sha256"], "content": content},
                                               create("tests/test_regression.py", "from src.calculator import average\ndef test_empty_regression(): assert average([]) == 0\n")],
                        reason="Guard empty input and add a regression", evidence_ids=[self.source["evidence"][0]["id"], data["evidence"][0]["id"]])
        if self.step == 4:
            return call("read_file", path="tests/test_regression.py")
        assert data["path"] == "tests/test_regression.py"
        return {"type": "final", "message": "Approved guard and regression applied; inspect actual test evidence."}


def test_real_repair_create_regression_scope_extension_and_restart_rollback(project, tmp_path, interpreter):
    policy, config = PolicyGuard(project), AgentConfig()
    files = ScopedFiles(policy, config, ["src/calculator.py", "tests/test_calculator.py", "pytest.ini"], Redactor())
    audit = AuditLog(tmp_path / "runs", project)
    output = []
    controller = AgentController(RegressionRepair(), ToolRegistry(files), audit, config, read_only=False,
                                 approve=lambda proposal: True, display=output.append,
                                 test_runner=TestRunner(policy, interpreter, config, authorized=True))
    result = controller.run("average([]) returns 0")
    assert result.status == "verified", audit.read_json("summary.json")
    assert "tests/test_regression.py" in files.allowed
    assert "tests/test_regression.py" in controller.diagnosis.reader.allowed
    assert any("authorizes" in message for message in output)
    assert "tests/test_regression.py::test_empty_regression" in controller.latest_test.cases
    restarted = PatchManager(policy, audit.directory, read_only=False)
    restarted.rollback_preview()
    restarted.rollback(True)
    assert not (project / "tests/test_regression.py").exists()
    assert "if not values" not in (project / "src/calculator.py").read_text()


def test_creation_decline_never_expands_scope_or_writes(project, tmp_path):
    policy, config = PolicyGuard(project), AgentConfig()
    files = ScopedFiles(policy, config, ["src/calculator.py", "tests/test_calculator.py"], Redactor())
    audit = AuditLog(tmp_path / "runs", project)
    result = AgentController(RegressionRepair(), ToolRegistry(files), audit, config, read_only=False,
                             approve=lambda proposal: False).run("bug")
    assert result.status == "cancelled"
    assert "tests/test_regression.py" not in files.allowed
    assert not (project / "tests/test_regression.py").exists()


def test_unsaved_editor_state_is_validated_and_used():
    interaction = PanelInteraction(io.StringIO(), io.StringIO())
    interaction.handle({"version": 1, "type": "editor_state", "dirty_paths": ["src/a.py"]})
    assert interaction.unsaved_paths() == {"src/a.py"}
    interaction.handle({"version": 1, "type": "editor_state", "dirty_paths": ["../a.py"]})
    assert interaction.unsaved_paths() == {"src/a.py"}
    interaction.handle({"version": 1, "type": "editor_state", "dirty_paths": []})
    assert not interaction.unsaved_paths()


def test_reproposal_preserves_user_content_and_requires_new_approval(tmp_path):
    root = tmp_path / "project"
    source = put(root, "a.py", "value = 1\n")
    decisions = []
    class Adapter:
        def __init__(self): self.step = 0
        def next_action(self, messages, schemas):
            self.step += 1
            data = next((message["result"]["data"] for message in reversed(messages) if message.get("role") == "tool"), {})
            if self.step in {1, 3}: return call("read_file", path="a.py")
            if self.step in {2, 4}:
                return call("propose_edits", edits=[{"path": "a.py", "original_sha256": data["sha256"], "content": data["content"].replace("value = 1", "value = 2")}],
                            reason="Change value preserving current content", evidence_ids=[data["evidence"][0]["id"]])
            return {"type": "final", "message": "Applied refreshed proposal"}
    def approve(proposal):
        decisions.append(proposal)
        if len(decisions) == 1: source.write_text("value = 1\n# user comment\n", encoding="utf-8")
        return True
    audit = AuditLog(tmp_path / "runs", root)
    controller = AgentController(Adapter(), ToolRegistry(FileTools(PolicyGuard(root))), audit, AgentConfig(),
                                 read_only=False, approve=approve)
    assert controller.run("value should be 2").status == "unverified"
    assert len(decisions) == 2 and "# user comment" in source.read_text()
    assert controller.repair_attempts == 2


def test_repair_attempt_budget_stops_further_proposals(tmp_path):
    root = tmp_path / "project"
    put(root, "a.py", "value = 1\n")
    class Adapter:
        def __init__(self): self.step = 0
        def next_action(self, messages, schemas):
            self.step += 1
            if self.step % 2: return call("read_file", path="a.py")
            data = messages[-1]["result"]["data"]
            return call("propose_edits", edits=[create()], reason="Add helper", evidence_ids=[data["evidence"][0]["id"]])
    config = AgentConfig(max_repair_attempts=1)
    audit = AuditLog(tmp_path / "runs", root)
    controller = AgentController(Adapter(), ToolRegistry(FileTools(PolicyGuard(root), config)), audit, config, read_only=True)
    result = controller.run("bug")
    assert result.reason == "repair_limit" and controller.repair_attempts == 1


def test_separate_expectation_decline_writes_none_of_the_proposal(tmp_path):
    root = tmp_path / "project"
    source = put(root, "service.py", "def current(): return 1\n")
    test = put(root, "test_service.py", "from service import current\ndef test_current(): assert current() == 1\n")
    original = source.read_bytes(), test.read_bytes()
    class Adapter:
        def __init__(self): self.step = 0
        def next_action(self, messages, schemas):
            self.step += 1
            data = next((message["result"]["data"] for message in reversed(messages) if message.get("role") == "tool"), {})
            if self.step == 1: return call("read_file", path="service.py")
            if self.step == 2:
                self.source = data
                return call("read_file", path="test_service.py")
            return call("propose_edits", edits=[{"path": "service.py", "content": self.source["content"].replace("return 1", "return 2"), "original_sha256": self.source["sha256"]},
                                               {"path": "test_service.py", "content": data["content"].replace("== 1", "== 2"), "original_sha256": data["sha256"]}],
                        reason="Requirement changes value", evidence_ids=[self.source["evidence"][0]["id"], data["evidence"][0]["id"]],
                        test_change_reason="Expected value changed from 1 to 2")
    approvals = []
    def approve_expected(proposal):
        approvals.append(proposal.expectation_changes)
        return False
    audit = AuditLog(tmp_path / "runs", root)
    result = AgentController(Adapter(), ToolRegistry(FileTools(PolicyGuard(root))), audit, AgentConfig(), read_only=False,
                             approve=lambda proposal: True, approve_test_changes=approve_expected).run("value should be 2")
    assert result.status == "cancelled" and approvals == [("test_service.py",)]
    assert (source.read_bytes(), test.read_bytes()) == original
    assert not (audit.directory / "manifest.json").exists()


def test_cancel_during_preflight_prevents_any_write(manager):
    proposal = manager.preview([replace(manager)])
    manager.consistency.cancelled = lambda: True
    with pytest.raises(ToolError) as error:
        manager.apply(proposal, True)
    assert error.value.code == "CANCELLED"
    assert not manager.manifest_path.exists()
