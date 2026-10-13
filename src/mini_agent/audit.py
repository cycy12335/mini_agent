"""Local audit records; the model never selects artifact paths."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .policy import _check_links


DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[2] / ".mini-agent" / "runs"


class AuditLog:
    @classmethod
    def open(cls, runs_root: Path, run_id: str):
        if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", run_id):
            raise ValueError("Invalid run ID")
        directory = runs_root.absolute() / run_id
        _check_links(directory)
        if not directory.is_dir():
            raise ValueError("Run directory does not exist")
        instance = cls.__new__(cls)
        instance.directory, instance.run_id = directory, run_id
        instance.events = directory / "events.jsonl"
        return instance

    def __init__(self, runs_root: Path, project: Path, *, redactor=None):
        self.redactor = redactor
        destination = runs_root.absolute()
        # Check existing ancestors before mkdir, including the destination itself.
        for parent in (*reversed(destination.parents), destination):
            if parent.exists() or parent.is_symlink():
                _check_links(parent)
        resolved = destination.resolve()
        if resolved.is_relative_to(project):
            raise ValueError("Run artifacts must be outside the target project")
        destination.mkdir(parents=True, exist_ok=True)
        _check_links(destination)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        self.directory = destination / self.run_id
        self.directory.mkdir()
        self.events = self.directory / "events.jsonl"

    def record(self, event: str, **data) -> None:
        entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        if getattr(self, "redactor", None):
            entry = self.redactor.value(entry)
        with self._path("events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _path(self, name: str) -> Path:
        if name not in {"events.jsonl", "proposals.json", "summary.json", "report.md", "proposal.diff", "manifest.json", "rollback.json", "test-output.txt"} and not re.fullmatch(r"(?:proposal-\d{3,}\.diff|test-\d{3,}(?:-results\.json|-output\.txt|\.json))", name):
            raise ValueError("Unsupported artifact name")
        path = self.directory / name
        _check_links(self.directory)
        if path.exists() or path.is_symlink():
            _check_links(path)
            if path.stat().st_nlink > 1:
                raise ValueError("Hard-linked artifacts are forbidden")
        return path

    def read_json(self, name: str):
        path = self._path(name)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Artifact exceeds read limit")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, name: str, text: str) -> None:
        from .patching import atomic_write
        if getattr(self, "redactor", None) and not name.endswith(".json"):
            text = self.redactor.text(text)
        atomic_write(self._path(name), text.encode("utf-8"))

    def write_json(self, name: str, value) -> None:
        # Called only with controller-owned constant filenames.
        if name not in {"proposals.json", "summary.json", "rollback.json"} and not re.fullmatch(r"test-\d{3,}(?:-results)?\.json", name):
            raise ValueError("Unsupported artifact name")
        if getattr(self, "redactor", None):
            value = self.redactor.value(value)
        self.write_text(name, json.dumps(value, ensure_ascii=False, indent=2))
