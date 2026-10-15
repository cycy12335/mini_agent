"""Versioned stdio UI adapter. The ordinary CLI and controller remain independent."""

import hashlib
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from .cli import main
from .models import ToolError


class PanelInteraction:
    def __init__(self, source, destination):
        self.source, self.destination = source, destination
        self._cancelled = threading.Event()
        self._lock, self._write_lock = threading.Lock(), threading.Lock()
        self._pending = None
        self._dirty_paths = set()
        self.seq = 0
        self.run_id = None
        self.redactor = None
        self.deadline = float("inf")

    def emit(self, message_type, **data):
        with self._write_lock:
            self.seq += 1
            envelope = {"version": 1, "seq": self.seq, "run_id": self.run_id,
                        "time": datetime.now(timezone.utc).isoformat(), "type": message_type, **data}
            if self.redactor:
                envelope = self.redactor.value(envelope)
            self.destination.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            self.destination.flush()

    def attach(self, audit, timeout, redactor):
        self.audit = audit
        self.run_id, self.redactor = audit.run_id, redactor
        self.deadline = time.monotonic() + timeout
        self.emit("run_ready", directory=str(audit.directory))

    def on_audit(self, entry):
        self.emit("audit", entry=entry)

    def display(self, text):
        self.emit("display", text=text)

    def cancelled(self):
        return self._cancelled.is_set()

    def unsaved_paths(self):
        with self._lock:
            return set(self._dirty_paths)

    def handle(self, command):
        if not isinstance(command, dict) or command.get("version") != 1:
            self.emit("command_error", message="Unsupported command version")
            return
        if command.get("type") == "cancel":
            self._cancelled.set()
            return
        if command.get("type") == "editor_state":
            paths = command.get("dirty_paths")
            from pathlib import PureWindowsPath
            if not isinstance(paths, list) or len(paths) > 1000 or any(
                not isinstance(path, str) or not path or len(path) > 1024
                or PureWindowsPath(path).drive or PureWindowsPath(path).root
                or any(part in {"", ".", ".."} for part in path.replace("\\", "/").split("/"))
                or any(ord(char) < 32 for char in path) for path in paths):
                self.emit("command_error", message="Invalid editor state")
                return
            with self._lock:
                self._dirty_paths = {path.replace("\\", "/") for path in paths}
            return
        with self._lock:
            pending = self._pending
            if (command.get("type") != "approval" or not pending
                    or command.get("approval_id") != pending["id"]
                    or ("proposal_version" in command and command["proposal_version"] != pending.get("version"))
                    or type(command.get("approved")) is not bool or pending["ready"].is_set()):
                self.emit("command_error", message="Invalid, stale or duplicate approval")
                return
            pending["approved"] = command["approved"]
            pending["ready"].set()

    def listen(self):
        try:
            while True:
                line = self.source.readline(16385)
                if not line:
                    break
                if len(line.encode("utf-8")) > 16384 or not line.endswith("\n"):
                    self._cancelled.set()
                    break
                try:
                    self.handle(json.loads(line))
                except (ValueError, TypeError):
                    self.emit("command_error", message="Invalid JSON command")
        except UnicodeError:
            self._cancelled.set()
        finally:
            self._cancelled.set()  # A disconnected client must never authorize a write.

    def request(self, kind, payload):
        version = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        pending = {"id": uuid.uuid4().hex, "version": version, "ready": threading.Event(), "approved": False}
        with self._lock:
            self._pending = pending
        consumed = False
        try:
            if getattr(self, "audit", None):
                self.audit.record("approval_requested", approval_id=pending["id"], proposal_version=version, kind=kind, payload=payload)
            self.emit("approval_requested", approval_id=pending["id"], proposal_version=version, kind=kind, payload=payload)
            while True:
                if self.cancelled():
                    raise ToolError("CANCELLED", "Cancelled while awaiting approval")
                if time.monotonic() >= self.deadline:
                    raise ToolError("TIME_LIMIT", "Approval exceeded the run time budget")
                if pending["ready"].wait(0.02):
                    consumed = True
                    return pending["approved"]
        finally:
            with self._lock:
                self._pending = None
            if getattr(self, "audit", None):
                self.audit.record("approval_resolved", approval_id=pending["id"], proposal_version=version,
                                  approved=consumed and pending["approved"] and not self.cancelled())
            self.emit("approval_resolved", approval_id=pending["id"], proposal_version=version)

    def proposal_payload(self, proposal):
        return {"diff": proposal.diff, "reason": proposal.reason, "evidence_ids": list(proposal.evidence_ids),
                "notice": "批准创建也授权本次运行后续按需读取新增文件。",
                "warnings": list(proposal.warnings), "files": [
            {"path": edit.path, "original": edit.original.decode("utf-8"),
             "replacement": edit.replacement.decode("utf-8"),
             "operation": edit.operation,
             "original_sha256": None if edit.operation == "create" else hashlib.sha256(edit.original).hexdigest()}
            for edit in proposal.edits]}

    def preview(self, proposal):
        self.emit("proposal", payload=self.proposal_payload(proposal))

    def approve(self, proposal):
        return self.request("proposal", self.proposal_payload(proposal))


def run(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    interaction = PanelInteraction(sys.stdin, sys.stdout)
    threading.Thread(target=interaction.listen, daemon=True).start()
    try:
        arguments = sys.argv[1:] if argv is None else argv
        operation = "rollback" if arguments and arguments[0] == "rollback" else "run"
        if operation == "rollback":
            arguments = arguments[1:]
        code = main([operation, *arguments], interaction=interaction)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except (BrokenPipeError, OSError):
        return 1
    interaction.emit("process_result", exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(run())
