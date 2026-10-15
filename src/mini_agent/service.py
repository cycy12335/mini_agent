"""Independent local task service. JSONL is a transport, not a model tool."""
import io
import json
from pathlib import Path
import sys
import threading
import uuid

from .audit import AuditLog
from .history import history, replay, task_status
from .panel import PanelInteraction
from .runtime import run_task, recover_run


class ServiceInteraction(PanelInteraction):
    def __init__(self, destination, task_id):
        super().__init__(io.StringIO(), destination)
        self.task_id = task_id

    def emit(self, message_type, **data):
        super().emit(message_type, task_id=self.task_id, **data)

    def handle(self, command):
        if command.get("type") == "approval" and not isinstance(command.get("proposal_version"), str):
            self.emit("command_error", message="Approval requires proposal_version")
            return
        super().handle(command)


class LockedWriter:
    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()

    def write(self, text):
        with self.lock:
            self.stream.write(text)
            self.stream.flush()

    def flush(self):
        pass


class CoreService:
    """Service lifetime owns cancellation; persisted runs outlive client processes."""
    def __init__(self, destination):
        self.destination = LockedWriter(destination)
        self.tasks = {}
        self.lock = threading.Lock()
        self.closed = False

    def start(self, arguments, *, operation="run"):
        if operation not in {"run", "rollback"}:
            raise ValueError("Only run and rollback tasks are supported")
        if not isinstance(arguments, list) or len(arguments) > 128 or any(not isinstance(arg, str) or len(arg) > 8192 for arg in arguments):
            raise ValueError("Invalid task arguments")
        from .cli import build_parser
        parser = build_parser()
        def error(message):
            raise ValueError(message)
        parser.error = error
        # Version/help are process controls, not task options.
        if any(arg in {"--help", "-h", "--version"} for arg in arguments):
            raise ValueError("Process flags cannot be submitted as tasks")
        args = parser.parse_args([operation, *arguments])
        task_id = uuid.uuid4().hex
        interaction = ServiceInteraction(self.destination, task_id)
        entry = {"interaction": interaction, "status": "queued", "exit_code": None}
        def worker():
            entry["status"] = "running"
            try:
                code = run_task(args, interaction=interaction, error=error, confirm=lambda _: False) if operation == "run" else recover_run(args, interaction=interaction)
            except Exception as exc:
                # Credential and source strings must not escape through arbitrary exception text.
                interaction.emit("task_error", error_code=type(exc).__name__)
                code = 1
            entry.update(status="completed", exit_code=code)
            interaction.emit("process_result", exit_code=code)
        with self.lock:
            if self.closed:
                raise ValueError("Service is closed")
            # Bound live threads and retained task handles.
            if sum(item["status"] != "completed" for item in self.tasks.values()) >= 16:
                raise ValueError("Too many active tasks")
            if len(self.tasks) >= 1000:
                for key in list(self.tasks):
                    if self.tasks[key]["status"] == "completed":
                        del self.tasks[key]
                        break
            entry["thread"] = threading.Thread(target=worker, daemon=True)
            self.tasks[task_id] = entry
            entry["thread"].start()
        return {"task_id": task_id}

    def command(self, command):
        if not isinstance(command, dict) or command.get("version") != 1:
            raise ValueError("Unsupported command version")
        kind = command.get("type")
        if kind in {"start", "recover"}:
            return self.start(command.get("argv"), operation="rollback" if kind == "recover" else "run")
        if kind == "history":
            return history(Path(command["runs_dir"]), limit=command.get("limit", 100))
        if kind in {"events", "run_status"}:
            audit = AuditLog.open(Path(command["runs_dir"]), command["run_id"])
            return replay(audit, after=command.get("after", 0), limit=command.get("limit", 100)) if kind == "events" else task_status(audit)
        with self.lock:
            entry = self.tasks.get(command.get("task_id"))
        if entry is None:
            raise ValueError("Unknown task ID")
        interaction = entry["interaction"]
        if kind == "status":
            with interaction._lock:
                pending = interaction._pending
                approval = {"approval_id": pending["id"], "proposal_version": pending["version"]} if pending else None
            return {"task_id": interaction.task_id, "run_id": interaction.run_id, "status": entry["status"],
                    "exit_code": entry["exit_code"], "approval": approval}
        if kind in {"cancel", "approval", "editor_state"}:
            interaction.handle(command)
            return {"task_id": interaction.task_id, "received": True}
        raise ValueError("Unknown service command")

    def close(self):
        with self.lock:
            self.closed = True
            entries = list(self.tasks.values())
        for entry in entries:
            entry["interaction"]._cancelled.set()
        # One shared deadline, not a timeout multiplied by task count.
        import time
        deadline = time.monotonic() + 5
        for entry in entries:
            entry["thread"].join(timeout=max(0, deadline - time.monotonic()))


def serve(source=None, destination=None):
    source, destination = source or sys.stdin, destination or sys.stdout
    if source is sys.stdin and hasattr(source, "reconfigure"):
        source.reconfigure(encoding="utf-8")
        destination.reconfigure(encoding="utf-8")
    service = CoreService(destination)
    try:
        while True:
            line = source.readline(65537)
            if not line:
                break
            if len(line.encode("utf-8")) > 65536 or not line.endswith("\n"):
                raise ValueError("Command exceeds framing limit")
            request_id = None
            try:
                command = json.loads(line)
                if isinstance(command, dict):
                    request_id = command.get("request_id")
                    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 128):
                        raise ValueError("Invalid request ID")
                result = service.command(command)
                response = {"version": 1, "type": "response", "request_id": request_id, "result": result}
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                response = {"version": 1, "type": "response", "request_id": request_id,
                            "error": getattr(exc, "code", "INVALID_COMMAND")}
            service.destination.write(json.dumps(response, ensure_ascii=False) + "\n")
    except (KeyboardInterrupt, ValueError, UnicodeError):
        return 3
    except (BrokenPipeError, OSError):
        return 1
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
