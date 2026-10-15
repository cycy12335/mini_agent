"""Exercise the actual stdio adapter, including edits, cancellation and conflicts."""
import io
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

from mini_agent.panel import PanelInteraction
from mini_agent.models import ToolError


class Session:
    def __init__(self, project, runs, *args, operation="run", run_id=None):
        repo = Path(__file__).resolve().parents[1]
        command = [sys.executable, "-u", "-m", "mini_agent.panel"]
        if operation == "rollback":
            command += ["rollback", "--run-id", run_id, "--runs-dir", str(runs)]
        else:
            command += ["--project", str(project), "--task", "修复 average([])", "--runs-dir", str(runs), *args]
        self.process = subprocess.Popen(command,
            cwd=project, env={**os.environ, "PYTHONPATH": str(repo / "src"), "PYTHONIOENCODING": "utf-8"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.events, self.all_events = queue.Queue(), []
        def read():
            for line in self.process.stdout:
                self.events.put(json.loads(line))
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def next(self, kind):
        deadline = time.monotonic() + 10
        while True:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            self.all_events.append(event)
            if event["type"] == kind:
                return event

    def send(self, **command):
        self.process.stdin.write(json.dumps({"version": 1, **command}) + "\n")
        self.process.stdin.flush()

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()
        self.reader.join(timeout=2)


@pytest.fixture
def demo(tmp_path):
    project, runs = tmp_path / "project", tmp_path / "runs"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "buggy_project", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return project, runs


@pytest.mark.parametrize("approved, expected", [(True, "unverified"), (False, "cancelled")])
def test_panel_approval_controls_actual_write(demo, approved, expected):
    project, runs = demo
    source = project / "src/calculator.py"
    original = source.read_bytes()
    session = Session(project, runs)
    try:
        request = session.next("approval_requested")
        assert request["kind"] == "proposal"
        assert request["payload"]["files"][0]["original"] == original.decode("utf-8")
        assert source.read_bytes() == original
        session.send(type="approval", approval_id="stale", approved=True)
        session.next("command_error")
        assert source.read_bytes() == original
        session.send(type="approval", approval_id=request["approval_id"], approved=approved)
        result = session.next("process_result")
        assert result["exit_code"] == (2 if approved else 3)
        assert session.process.wait(timeout=5) == result["exit_code"]
        summary = json.loads(next(runs.glob("*/summary.json")).read_text(encoding="utf-8"))
        assert summary["status"] == expected
        assert (source.read_bytes() != original) is approved
        assert [e["seq"] for e in session.all_events] == sorted(set(e["seq"] for e in session.all_events))
    finally:
        session.close()


@pytest.mark.parametrize("disconnect", [False, True])
def test_cancel_or_disconnect_while_waiting_never_writes(demo, disconnect):
    project, runs = demo
    original = (project / "src/calculator.py").read_bytes()
    session = Session(project, runs)
    try:
        session.next("approval_requested")
        if disconnect:
            session.process.stdin.close()
        else:
            session.send(type="cancel")
        assert session.next("process_result")["exit_code"] == 3
        assert (project / "src/calculator.py").read_bytes() == original
        assert not list(runs.glob("*/manifest.json"))
    finally:
        session.close()


def test_changed_file_invalidates_approved_proposal(demo):
    project, runs = demo
    source = project / "src/calculator.py"
    session = Session(project, runs)
    try:
        request = session.next("approval_requested")
        changed = source.read_bytes() + b"\n# user edit\n"
        source.write_bytes(changed)
        session.send(type="approval", approval_id=request["approval_id"], approved=True)
        assert session.next("process_result")["exit_code"] == 1
        assert source.read_bytes() == changed
        summary = json.loads(next(runs.glob("*/summary.json")).read_text(encoding="utf-8"))
        assert summary["reason"] == "hash_conflict"
    finally:
        session.close()


def test_readonly_emits_native_preview_without_approval(demo):
    session = Session(*demo, "--read-only")
    try:
        preview = session.next("proposal")
        assert "if not values:" in preview["payload"]["files"][0]["replacement"]
        assert session.next("process_result")["exit_code"] == 0
        assert not any(e["type"] == "approval_requested" for e in session.all_events)
    finally:
        session.close()


def test_approval_is_boolean_single_use_and_time_bounded():
    interaction = PanelInteraction(io.StringIO(), io.StringIO())
    ready = threading.Event()
    interaction._pending = {"id": "id", "approved": False, "ready": ready}
    interaction.handle({"version": 1, "type": "approval", "approval_id": "id", "approved": "true"})
    assert not ready.is_set()
    interaction.handle({"version": 1, "type": "approval", "approval_id": "id", "approved": False})
    interaction.handle({"version": 1, "type": "approval", "approval_id": "id", "approved": True})
    assert interaction._pending["approved"] is False
    interaction.deadline = time.monotonic() - 1
    with pytest.raises(ToolError, match="time budget"):
        interaction.request("proposal", {})
    assert interaction._pending is None


def test_disclosure_decline_stops_before_model_request(demo):
    project, runs = demo
    repo = Path(__file__).resolve().parents[1]
    config = project / "config.json"
    config.write_bytes((repo / "agent.api.example.json").read_bytes())
    key = "fake-panel-test-key-never-send"
    (project / "agent.credentials.local.json").write_text(json.dumps({"api_key": key}), encoding="utf-8")
    session = Session(project, runs, "--model", "api", "--config", str(config), "--read-only")
    try:
        request = session.next("approval_requested")
        assert request["kind"] == "disclosure"
        assert request["payload"]["endpoint"]
        assert "src/calculator.py" in request["payload"]["files"]
        session.send(type="approval", approval_id=request["approval_id"], approved=False)
        assert session.next("process_result")["exit_code"] == 3
        serialized = json.dumps(session.all_events)
        assert key not in serialized
        assert not any(e["type"] == "audit" and e["entry"]["event"] in {"model_cost", "action", "tests"} for e in session.all_events)
    finally:
        session.close()


def applied_run(demo):
    project, runs = demo
    original = (project / "src/calculator.py").read_bytes()
    session = Session(project, runs)
    try:
        request = session.next("approval_requested")
        session.send(type="approval", approval_id=request["approval_id"], approved=True)
        assert session.next("process_result")["exit_code"] == 2
        assert session.process.wait(timeout=5) == 2
    finally:
        session.close()
    return next(runs.iterdir()), original


@pytest.mark.parametrize("decision", ["approve", "decline", "cancel"])
def test_restore_requires_approval_and_keeps_historical_report(demo, decision):
    directory, original = applied_run(demo)
    report = (directory / "report.md").read_bytes()
    source = demo[0] / "src/calculator.py"
    modified = source.read_bytes()
    session = Session(*demo, operation="rollback", run_id=directory.name)
    try:
        request = session.next("approval_requested")
        assert request["kind"] == "rollback"
        assert request["payload"]["files"] == ["src/calculator.py"]
        assert source.read_bytes() == modified, "Preview must not restore files"
        if decision == "cancel":
            session.send(type="cancel")
        else:
            session.send(type="approval", approval_id=request["approval_id"], approved=decision == "approve")
        result = session.next("recovery_result")
        assert result["status"] == ("rolled_back" if decision == "approve" else "cancelled")
        assert session.next("process_result")["exit_code"] == (0 if decision == "approve" else 3)
        assert source.read_bytes() == (original if decision == "approve" else modified)
        assert (directory / "report.md").read_bytes() == report
        assert json.loads((directory / "summary.json").read_text(encoding="utf-8"))["status"] == "unverified"
    finally:
        session.close()
    if decision == "approve":
        again = Session(*demo, operation="rollback", run_id=directory.name)
        try:
            assert again.next("recovery_result")["status"] == "nothing_to_restore"
            assert again.next("process_result")["exit_code"] == 0
        finally:
            again.close()


@pytest.mark.parametrize("change", ["before_preview", "after_preview", "backup", "manifest"])
def test_restore_refuses_conflicts_and_corrupt_backup(demo, change):
    directory, _ = applied_run(demo)
    source = demo[0] / "src/calculator.py"
    changed = source.read_bytes() + b"\n# later user change\n"
    if change == "before_preview":
        source.write_bytes(changed)
    if change == "backup":
        next((directory / "backups").iterdir()).write_bytes(b"corrupt")
    session = Session(*demo, operation="rollback", run_id=directory.name)
    try:
        if change in {"after_preview", "manifest"}:
            request = session.next("approval_requested")
            if change == "after_preview":
                source.write_bytes(changed)
            else:
                manifest = directory / "manifest.json"
                manifest.write_bytes(manifest.read_bytes() + b"\n")
            session.send(type="approval", approval_id=request["approval_id"], approved=True)
        before = source.read_bytes()
        result = session.next("recovery_result")
        assert result["status"] == "failed"
        assert result["error_code"] == {"backup": "BACKUP_CORRUPT", "manifest": "PROPOSAL_CHANGED"}.get(change, "HASH_CONFLICT")
        assert session.next("process_result")["exit_code"] == 1
        assert source.read_bytes() == before
    finally:
        session.close()


def test_restore_incomplete_manifest_without_summary(demo):
    directory, original = applied_run(demo)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["transactions"][0]["status"] = "prepared"
    manifest["transactions"][0]["files"][0]["state"] = "writing"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "summary.json").unlink()
    session = Session(*demo, operation="rollback", run_id=directory.name)
    try:
        request = session.next("approval_requested")
        session.send(type="approval", approval_id=request["approval_id"], approved=True)
        assert session.next("recovery_result")["status"] == "rolled_back"
        assert (demo[0] / "src/calculator.py").read_bytes() == original
        assert not (directory / "summary.json").exists()
    finally:
        session.close()
