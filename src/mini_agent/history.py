"""Durable task ownership, status and bounded event replay; no automatic resume."""
from datetime import datetime, timezone
import json
import os

from .audit import AuditLog
from .locking import FileLease
from .models import ToolError


def now():
    return datetime.now(timezone.utc).isoformat()


class TaskLifecycle:
    def __init__(self, audit, project):
        self.audit, self.project = audit, project

    def __enter__(self):
        self.lease = FileLease(self.audit._path("owner.lock"))
        self.lease.__enter__()
        try:
            self.audit.write_json("task.json", {"version": 1, "run_id": self.audit.run_id,
                "project": str(self.project), "pid": os.getpid(), "status": "running", "started": now(),
                "resumable": False})
            self.audit.record("task_running", project=str(self.project))
            return self
        except BaseException:
            self.lease.__exit__()
            raise

    def __exit__(self, exc_type, *args):
        try:
            state = self.audit.read_json("task.json")
            summary = self.audit.read_json("summary.json") if self.audit._path("summary.json").exists() else None
            state.update(status=summary["status"] if summary else "failed", ended=now(),
                         reason=summary["reason"] if summary else "execution_incomplete")
            self.audit.write_json("task.json", state)
            self.audit.record("task_stopped", status=state["status"], reason=state["reason"])
        finally:
            self.lease.__exit__()


def task_status(audit):
    if audit._path("task.json").exists():
        state = audit.read_json("task.json")
    else:
        summary = audit.read_json("summary.json") if audit._path("summary.json").exists() else {}
        state = {"run_id": audit.run_id, "status": summary.get("status", "unknown"),
                 "legacy": True, "resumable": False}
    if state["status"] == "running":
        try:
            with FileLease(audit._path("owner.lock")):
                # Re-read after acquiring ownership: another process may have just finished.
                state = audit.read_json("task.json")
                if state["status"] == "running":
                    state = {**state, "status": "interrupted", "reason": "owner_disconnected", "resumable": False}
        except ToolError as exc:
            if exc.code != "REPOSITORY_BUSY":
                raise
    manifest = audit.read_json("manifest.json") if audit._path("manifest.json").exists() else None
    state["recovery_available"] = bool(manifest and any(
        tx.get("status") not in {"recovered", "rolled_back"} and any(
            file.get("state") in {"writing", "written"} for file in tx.get("files", []))
        for tx in manifest.get("transactions", [])))
    return state


def history(runs_root, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("History limit must be 1–1000")
    if not runs_root.exists():
        return []
    from .policy import _check_links
    _check_links(runs_root)
    import re
    names = sorted((p.name for p in runs_root.iterdir() if re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", p.name)), reverse=True)
    return [task_status(AuditLog.open(runs_root, name)) for name in names[:limit]]


def replay(audit, *, after=0, limit=100):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid event cursor or limit")
    path = audit._path("events.jsonl")
    if not path.exists():
        return {"events": [], "next": after}
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Event log exceeds replay read limit")
    events = []
    with path.open("rb") as stream:
        for seq, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                break  # An interrupted append is not an event.
            if seq > after:
                entry = json.loads(line.decode("utf-8"))
                events.append({**entry, "seq": seq})
                if len(events) >= limit:
                    break
    return {"events": events, "next": events[-1]["seq"] if events else after}
