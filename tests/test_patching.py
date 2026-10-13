import json
from pathlib import Path

import pytest

from mini_agent.models import ToolError
from mini_agent.config import AgentConfig
from mini_agent.patching import PatchManager, digest
from mini_agent.policy import PolicyGuard


@pytest.fixture
def manager(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_bytes(b"value = 1\n")
    (project / "b.py").write_bytes(b"value = 2\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return PatchManager(PolicyGuard(project), artifacts, read_only=False)


def edit(manager, name="a.py", content="value = 3\n"):
    return {"path": name, "original_sha256": digest((manager.policy.root / name).read_bytes()), "content": content}


def test_preview_rejection_and_read_only_do_not_write(manager):
    before = (manager.policy.root / "a.py").read_bytes()
    proposal = manager.preview([edit(manager)])
    assert "--- a/a.py" in proposal.diff and "-value = 1" in proposal.diff and "+value = 3" in proposal.diff
    with pytest.raises(ToolError, match="approval"):
        manager.apply(proposal, False)
    manager.read_only = True
    with pytest.raises(ToolError, match="cannot modify"):
        manager.apply(proposal, True)
    assert (manager.policy.root / "a.py").read_bytes() == before
    assert not manager.manifest_path.exists()


def test_apply_backup_exact_bytes_and_rollback_after_restart(manager):
    original = b"\xef\xbb\xbfvalue = 1\r\n"
    (manager.policy.root / "a.py").write_bytes(original)
    proposal = manager.preview([edit(manager, content="value = 3\r\n")])
    assert manager.apply(proposal, True) == ["a.py"]
    manifest = json.loads(manager.manifest_path.read_text())
    entry = manifest["transactions"][0]["files"][0]
    assert (manager.directory / entry["backup"]).read_bytes() == original
    assert entry["original_sha256"] == digest(original)
    assert entry["new_sha256"] == digest(b"value = 3\r\n")
    restarted = PatchManager(manager.policy, manager.directory, read_only=False)
    assert restarted.rollback_preview() == ["a.py"]
    assert restarted.rollback(True) == ["a.py"]
    assert (manager.policy.root / "a.py").read_bytes() == original
    assert restarted.rollback_preview() == []
    assert restarted.rollback(True) == []


def test_hash_conflict_after_approval_does_not_overwrite(manager):
    proposal = manager.preview([edit(manager)])
    (manager.policy.root / "a.py").write_bytes(b"human = 4\n")
    with pytest.raises(ToolError, match="Content changed"):
        manager.apply(proposal, True)
    assert (manager.policy.root / "a.py").read_bytes() == b"human = 4\n"
    assert not manager.manifest_path.exists()


@pytest.mark.parametrize("failure", [OSError("simulated disk error"), KeyboardInterrupt()])
def test_partial_apply_failure_restores_all_changes(manager, monkeypatch, failure):
    proposal = manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")])
    original = manager._replace_file
    def fail_second(name, content, mode, expected_hash):
        if name == "b.py":
            raise failure
        original(name, content, mode, expected_hash)
    monkeypatch.setattr(manager, "_replace_file", fail_second)
    with pytest.raises(type(failure)):
        manager.apply(proposal, True)
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 1\n"
    assert (manager.policy.root / "b.py").read_bytes() == b"value = 2\n"
    assert manager.manifest["transactions"][0]["status"] == "recovered"
    assert not manager.modified_files()
    assert not list(manager.policy.root.glob(".mini-agent-*"))


def test_recovery_preserves_human_edit_on_partial_failure(manager, monkeypatch):
    proposal = manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")])
    original = manager._replace_file
    def conflict(name, content, mode, expected_hash):
        if name == "b.py":
            (manager.policy.root / "a.py").write_bytes(b"human = 9\n")
            raise OSError("write failed")
        original(name, content, mode, expected_hash)
    monkeypatch.setattr(manager, "_replace_file", conflict)
    with pytest.raises(ToolError, match="manual recovery"):
        manager.apply(proposal, True)
    assert (manager.policy.root / "a.py").read_bytes() == b"human = 9\n"
    assert manager.manifest["transactions"][0]["status"] == "recovery_conflict"


def test_all_backups_and_journal_exist_before_first_write(manager, monkeypatch):
    proposal = manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")])
    original = manager._replace_file
    def checked(name, content, mode, expected_hash):
        manifest = json.loads(manager.manifest_path.read_text())
        assert manifest["transactions"][0]["status"] == "prepared"
        assert len(list((manager.directory / "backups").glob("*.bin"))) == 2
        original(name, content, mode, expected_hash)
    monkeypatch.setattr(manager, "_replace_file", checked)
    manager.apply(proposal, True)


def test_rollback_preflight_conflict_changes_nothing(manager):
    manager.apply(manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")]), True)
    (manager.policy.root / "a.py").write_bytes(b"human = 9\n")
    with pytest.raises(ToolError, match="Later changes"):
        manager.rollback_preview()
    assert (manager.policy.root / "b.py").read_bytes() == b"value = 4\n"


def test_rollback_requires_preview_and_approval(manager):
    manager.apply(manager.preview([edit(manager)]), True)
    with pytest.raises(ToolError, match="Preview"):
        manager.rollback(True)
    manager.rollback_preview()
    with pytest.raises(ToolError, match="approval"):
        manager.rollback(False)
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 3\n"


def test_rollback_rechecks_manifest_after_approval(manager):
    manager.apply(manager.preview([edit(manager)]), True)
    manager.rollback_preview()
    manifest = json.loads(manager.manifest_path.read_text())
    manifest["extra"] = "changed during approval"
    manager.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ToolError, match="manifest changed"):
        manager.rollback(True)
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 3\n"


def test_multiple_transactions_on_same_file_restore_initial_content(manager):
    manager.apply(manager.preview([edit(manager)]), True)
    manager.apply(manager.preview([edit(manager, content="value = 5\n")]), True)
    assert manager.rollback_preview() == ["a.py"]
    manager.rollback(True)
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 1\n"


def test_corrupt_backup_refuses_recovery(manager):
    manager.apply(manager.preview([edit(manager)]), True)
    entry = manager.manifest["transactions"][0]["files"][0]
    (manager.directory / entry["backup"]).write_bytes(b"corrupt")
    with pytest.raises(ToolError, match="Backup failed"):
        manager.rollback_preview()
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 3\n"


def test_tampered_backup_path_is_rejected(manager):
    manager.apply(manager.preview([edit(manager)]), True)
    manager.manifest["transactions"][0]["files"][0]["backup"] = "../outside.py"
    manager._save()
    with pytest.raises(ToolError, match="Invalid backup"):
        PatchManager(manager.policy, manager.directory, read_only=False)


def test_link_replacement_after_preview_is_rejected(manager, tmp_path):
    proposal = manager.preview([edit(manager)])
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside = 7\n")
    probe = manager.policy.root / "probe"
    try:
        probe.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    probe.unlink()
    (manager.policy.root / "a.py").unlink()
    (manager.policy.root / "a.py").symlink_to(outside)
    with pytest.raises(ToolError, match="links"):
        manager.apply(proposal, True)
    assert outside.read_bytes() == b"outside = 7\n"


@pytest.mark.parametrize("content", ["", "def test_one():\n    assert 1 == 2\n",
    "import pytest\n@pytest.mark.skip\ndef test_one():\n    assert 1 == 1\n"])
def test_test_weakening_is_rejected(manager, content):
    (manager.policy.root / "test_one.py").write_text("def test_one():\n    assert 1 == 1\n")
    with pytest.raises(ToolError, match="Existing test"):
        manager.preview([edit(manager, "test_one.py", content)])


def test_added_tests_are_highlighted(manager):
    original = "def test_one():\n    assert 1 == 1\n"
    (manager.policy.root / "test_one.py").write_text(original)
    proposal = manager.preview([edit(manager, "test_one.py", original + "\ndef test_two():\n    assert 2 == 2\n")])
    assert "# TEST CHANGE: test_one.py" in proposal.diff


def test_no_newline_diff_and_noop(manager):
    proposal = manager.preview([edit(manager, content="value = 3")])
    assert "\\ No newline at end of file" in proposal.diff
    with pytest.raises(ToolError, match="no changes"):
        manager.preview([edit(manager, content="value = 1\n")])


def test_unwritten_file_changed_to_proposed_content_is_not_restored(manager, monkeypatch):
    proposal = manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")])
    original = manager._replace_file
    def change_future_file(name, content, mode, expected_hash):
        original(name, content, mode, expected_hash)
        if name == "a.py" and content == b"value = 3\n":
            (manager.policy.root / "b.py").write_bytes(b"value = 4\n")
    monkeypatch.setattr(manager, "_replace_file", change_future_file)
    with pytest.raises(ToolError, match="Content changed"):
        manager.apply(proposal, True)
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 1\n"
    assert (manager.policy.root / "b.py").read_bytes() == b"value = 4\n"
    assert manager.manifest["transactions"][0]["files"][1]["state"] == "pending"


def test_late_guard_conflict_does_not_restore_unwritten_file(manager, monkeypatch):
    proposal = manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")])
    original = manager._replace_file
    def change_before_replace(name, content, mode, expected_hash):
        if name == "b.py":
            (manager.policy.root / "b.py").write_bytes(content)
        original(name, content, mode, expected_hash)
    monkeypatch.setattr(manager, "_replace_file", change_before_replace)
    with pytest.raises(ToolError, match="Content changed"):
        manager.apply(proposal, True)
    assert (manager.policy.root / "b.py").read_bytes() == b"value = 4\n"
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 1\n"


def test_custom_file_budget_survives_restart(manager):
    original = b"#" + b"x" * 70000 + b"\n"
    (manager.policy.root / "a.py").write_bytes(original)
    larger = PatchManager(manager.policy, manager.directory, AgentConfig(max_file_bytes=100000), read_only=False)
    larger.apply(larger.preview([edit(larger)]), True)
    restarted = PatchManager(manager.policy, manager.directory, read_only=False)
    restarted.rollback_preview()
    restarted.rollback(True)
    assert (manager.policy.root / "a.py").read_bytes() == original


def test_interrupted_rollback_can_resume_without_reverting_human_changes(manager, monkeypatch):
    manager.apply(manager.preview([edit(manager), edit(manager, "b.py", "value = 4\n")]), True)
    original = manager._replace_file
    def fail_one(name, content, mode, expected_hash):
        if name == "a.py":
            raise OSError("cannot restore this file yet")
        original(name, content, mode, expected_hash)
    monkeypatch.setattr(manager, "_replace_file", fail_one)
    manager.rollback_preview()
    with pytest.raises(ToolError, match="incomplete"):
        manager.rollback(True)
    assert (manager.policy.root / "b.py").read_bytes() == b"value = 2\n"
    # Once b.py is restored, a later manual change must not be touched by retry.
    (manager.policy.root / "b.py").write_bytes(b"human = 8\n")
    restarted = PatchManager(manager.policy, manager.directory, read_only=False)
    assert restarted.rollback_preview() == ["a.py"]
    restarted.rollback(True)
    assert (manager.policy.root / "a.py").read_bytes() == b"value = 1\n"
    assert (manager.policy.root / "b.py").read_bytes() == b"human = 8\n"
