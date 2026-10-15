"""Core-6 process ownership, transport and restart recovery contracts."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from mini_agent.audit import AuditLog
from mini_agent.history import history, replay, task_status
from mini_agent.locking import RepositoryLease
from mini_agent.models import ToolError
from mini_agent.service import CoreService, serve


ROOT = Path(__file__).resolve().parents[1]


class Sink:
    def __init__(self):
        self.condition = threading.Condition()
        self.events = []

    def write(self, line):
        with self.condition:
            self.events.append(json.loads(line))
            self.condition.notify_all()

    def flush(self):
        pass

    def wait(self, kind, task_id=None, timeout=10):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for event in self.events:
                    if event["type"] == kind and (task_id is None or event.get("task_id") == task_id):
                        return event
                remaining = deadline - time.monotonic()
                assert remaining > 0, self.events
                self.condition.wait(remaining)


@pytest.fixture
def demo(tmp_path):
    project, runs = tmp_path / "project", tmp_path / "runs"
    shutil.copytree(ROOT / "examples/buggy_project", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mini-agent"))
    return project, runs


def arguments(demo):
    project, runs = demo
    return ["--project", str(project), "--runs-dir", str(runs), "--task", "average([]) should return 0"]


def command(service, kind, **data):
    return service.command({"version": 1, "type": kind, **data})


def approve(service, task, request, **overrides):
    return command(service, "approval", task_id=task, approval_id=request["approval_id"],
                   proposal_version=overrides.pop("proposal_version", request["proposal_version"]),
                   approved=True, **overrides)


def test_service_runs_approves_replays_and_recovers_without_cli_main(demo, monkeypatch):
    monkeypatch.setattr("mini_agent.cli.main", lambda *a, **k: pytest.fail("Service must use runtime"))
    sink, original = Sink(), (demo[0] / "src/calculator.py").read_bytes()
    service = CoreService(sink)
    try:
        task = command(service, "start", argv=arguments(demo))["task_id"]
        request = sink.wait("approval_requested", task)
        state = command(service, "status", task_id=task)
        assert state["approval"]["proposal_version"] == request["proposal_version"]
        assert command(service, "run_status", runs_dir=str(demo[1]), run_id=state["run_id"])["status"] == "running"
        approve(service, task, request, proposal_version="stale")
        sink.wait("command_error", task)
        assert (demo[0] / "src/calculator.py").read_bytes() == original
        assert not command(service, "history", runs_dir=str(demo[1]))[0]["recovery_available"]
        approve(service, task, request)
        assert sink.wait("process_result", task)["exit_code"] == 2
        rows = command(service, "history", runs_dir=str(demo[1]))
        assert rows[0]["status"] == "unverified"
        assert rows[0]["recovery_available"]
        events = command(service, "events", runs_dir=str(demo[1]), run_id=state["run_id"], limit=1000)["events"]
        approvals = [e for e in events if e["event"] == "approval_requested"]
        assert approvals[0]["proposal_version"] == request["proposal_version"]
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        recover = command(service, "recover", argv=["--runs-dir", str(demo[1]), "--run-id", state["run_id"]])["task_id"]
        recovery = sink.wait("approval_requested", recover)
        assert recovery["kind"] == "rollback"
        approve(service, recover, recovery)
        assert sink.wait("process_result", recover)["exit_code"] == 0
        assert (demo[0] / "src/calculator.py").read_bytes() == original
        assert not command(service, "history", runs_dir=str(demo[1]))[0]["recovery_available"]
    finally:
        service.close()


@pytest.mark.parametrize("disconnect", [False, True])
def test_service_cancel_and_close_interrupt_waiting_approval(demo, disconnect):
    sink, service = Sink(), None
    service = CoreService(sink)
    original = (demo[0] / "src/calculator.py").read_bytes()
    try:
        task = service.start(arguments(demo))["task_id"]
        sink.wait("approval_requested", task)
        service.close() if disconnect else command(service, "cancel", task_id=task)
        assert sink.wait("process_result", task)["exit_code"] == 3
        assert (demo[0] / "src/calculator.py").read_bytes() == original
    finally:
        service.close()


def test_same_repository_tasks_and_recovery_are_serialized(demo):
    sink, service = Sink(), None
    service = CoreService(sink)
    try:
        first = service.start(arguments(demo))["task_id"]
        sink.wait("approval_requested", first)
        second = service.start(arguments(demo))["task_id"]
        time.sleep(.1)
        assert command(service, "status", task_id=second)["run_id"] is None
        command(service, "cancel", task_id=second)
        assert sink.wait("process_result", second)["exit_code"] == 3
        assert len(list(demo[1].iterdir())) == 1
        command(service, "cancel", task_id=first)
        assert sink.wait("process_result", first)["exit_code"] == 3
        third = service.start([*arguments(demo), "--read-only"])["task_id"]
        assert sink.wait("process_result", third)["exit_code"] == 0
    finally:
        service.close()


def test_different_repositories_can_wait_for_approval_together(demo, tmp_path):
    other = tmp_path / "other"
    shutil.copytree(demo[0], other)
    sink, service = Sink(), None
    service = CoreService(sink)
    try:
        first = service.start(arguments(demo))["task_id"]
        sink.wait("approval_requested", first)
        second = service.start(arguments((other, tmp_path / "other-runs")))["task_id"]
        request = sink.wait("approval_requested", second)
        assert request["approval_id"] != sink.wait("approval_requested", first)["approval_id"]
        command(service, "cancel", task_id=first)
        command(service, "cancel", task_id=second)
    finally:
        service.close()


def test_os_lock_process_death_and_interrupted_write_recovery(demo, monkeypatch):
    project, runs = demo
    original = (project / "src/calculator.py").read_bytes()
    script = '''
import sys, time
from pathlib import Path
from mini_agent.audit import AuditLog
from mini_agent.history import TaskLifecycle
from mini_agent.locking import RepositoryLease
from mini_agent.patching import PatchManager, digest
from mini_agent.policy import PolicyGuard
project, runs = map(Path, sys.argv[1:])
policy = PolicyGuard(project)
with RepositoryLease(project):
    audit = AuditLog(runs, policy.root)
    with TaskLifecycle(audit, policy.root):
        patches = PatchManager(policy, audit.directory, read_only=False)
        source = (project / 'src/calculator.py').read_bytes()
        proposal = patches.preview([{'path': 'src/calculator.py', 'original_sha256': digest(source), 'content': source.decode() + '\\n# applied before interruption\\n'}])
        patches.apply(proposal, True)
        print(audit.run_id, flush=True)
        time.sleep(120)
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(project), str(runs)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        run_id = child.stdout.readline().strip()
        assert run_id, child.stderr.read()
        assert task_status(AuditLog.open(runs, run_id))["status"] == "running"
        with pytest.raises(ToolError) as error:
            with RepositoryLease(project):
                pass
        assert error.value.code == "REPOSITORY_BUSY"
        child.kill()
        child.wait(timeout=5)
        status = task_status(AuditLog.open(runs, run_id))
        assert status["status"] == "interrupted" and status["recovery_available"]
        with RepositoryLease(project):
            pass
        from mini_agent.cli import main
        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert main(["rollback", "--runs-dir", str(runs), "--run-id", run_id]) == 0
        assert (project / "src/calculator.py").read_bytes() == original
        assert history(runs)[0]["status"] == "interrupted"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


def test_replay_cursor_partial_tail_and_legacy_events(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    audit = AuditLog(tmp_path / "runs", project)
    audit.events.write_text('{"event":"legacy"}\n', encoding="utf-8")
    reopened = AuditLog.open(tmp_path / "runs", audit.run_id)
    reopened.record("second")
    first = replay(reopened, limit=1)
    assert first["next"] == 1
    second = replay(reopened, after=first["next"])
    assert second["next"] == 2 and second["events"][0]["event"] == "second"
    with reopened.events.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"partial')
    assert replay(reopened, after=2)["events"] == []
    reopened.record("after_crash")
    assert replay(reopened, after=2)["events"][0]["event"] == "after_crash"
    assert reopened._path("events-partial.txt").read_text() == '{"event":"partial'


def test_service_cancel_reaches_model(demo, monkeypatch):
    from mini_agent.adapters.mock import MockAdapter
    from mini_agent.models import ToolError
    entered = threading.Event()
    class WaitingAdapter(MockAdapter):
        def set_run_budget(self, remaining, cancelled):
            self.cancelled = cancelled
        def next_action(self, context, schemas):
            entered.set()
            deadline = time.monotonic() + 5
            while not self.cancelled() and time.monotonic() < deadline:
                time.sleep(.01)
            assert self.cancelled()
            raise ToolError("CANCELLED", "Model cancelled")
    monkeypatch.setattr("mini_agent.runtime.MockAdapter", WaitingAdapter)
    sink, service = Sink(), None
    service = CoreService(sink)
    try:
        task = service.start(arguments(demo))["task_id"]
        assert entered.wait(5)
        command(service, "cancel", task_id=task)
        assert sink.wait("process_result", task)["exit_code"] == 3
        assert history(demo[1])[0]["status"] == "cancelled"
    finally:
        service.close()


def test_service_cancel_reaches_real_pytest(demo):
    project, runs = demo
    (project / "tests/test_wait.py").write_text(
        "from pathlib import Path\nimport time\ndef test_wait():\n    Path('test_ready.txt').write_text('ready')\n    time.sleep(30)\n", encoding="utf-8")
    sink, service = Sink(), None
    service = CoreService(sink)
    try:
        task = service.start([*arguments(demo), "--allow-tests", "--test-python", sys.executable])["task_id"]
        deadline = time.monotonic() + 10
        while not (project / "test_ready.txt").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (project / "test_ready.txt").exists(), sink.events
        command(service, "cancel", task_id=task)
        assert sink.wait("process_result", task)["exit_code"] == 3
        summary = json.loads(next(runs.glob("*/summary.json")).read_text(encoding="utf-8"))
        assert summary["test_history"][0]["cancelled"]
        assert summary["test_history"][0]["cleanup_ok"]
    finally:
        service.close()


@pytest.mark.parametrize("data", [{"after": -1}, {"after": True}, {"limit": 0}, {"limit": 1001}])
def test_replay_rejects_invalid_cursors(tmp_path, data):
    project = tmp_path / "project"
    project.mkdir()
    audit = AuditLog(tmp_path / "runs", project)
    with pytest.raises(ValueError):
        replay(audit, **data)


def test_stdio_service_routes_responses_and_rejects_untrusted_commands():
    output = io.StringIO()
    source = io.StringIO('\n'.join(json.dumps(c) for c in [
        {"version": 1, "type": "start", "argv": ["--help"], "request_id": "a"},
        {"version": 1, "type": "shell", "request_id": "b"},
        {"version": 9, "type": "start", "request_id": "c"}]) + '\n')
    assert serve(source, output) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r["request_id"] for r in responses] == ["a", "b", "c"]
    assert all(r["error"] == "INVALID_COMMAND" for r in responses)


def test_lock_rejects_hardlinks_and_cancelled_wait(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with RepositoryLease(project):
        with pytest.raises(ToolError) as exc:
            with RepositoryLease(project, cancelled=lambda: True, timeout=10):
                pass
        assert exc.value.code == "CANCELLED"
    original = tmp_path / "original"
    original.write_bytes(b"0")
    (project / ".mini-agent/core.lock").unlink()
    os.link(original, project / ".mini-agent/core.lock")
    with pytest.raises(ValueError, match="Hard-linked"):
        with RepositoryLease(project):
            pass


def test_interrupted_utf8_append_keeps_complete_events_replayable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    audit = AuditLog(tmp_path / "runs", project)
    audit.record("before_crash")
    with audit.events.open("ab") as stream:
        stream.write(b'{"event":"' + "中".encode("utf-8")[:2])
    reopened = AuditLog.open(tmp_path / "runs", audit.run_id)
    assert replay(reopened)["events"][0]["event"] == "before_crash"
    reopened.record("recovered")
    assert replay(reopened)["next"] == 2


def test_legacy_history_uses_recorded_status_without_claiming_resume(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    audit = AuditLog(tmp_path / "runs", project)
    audit.write_json("summary.json", {"status": "unverified"})
    assert history(tmp_path / "runs")[0]["status"] == "unverified"
    assert history(tmp_path / "runs")[0]["legacy"]
    assert not history(tmp_path / "runs")[0]["resumable"]
