"""Local audit records; the model never selects artifact paths."""

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .policy import _check_links


DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[2] / ".mini-agent" / "runs"


class AuditLog:
    def __init__(self, runs_root: Path, project: Path):
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
        with self.events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_json(self, name: str, value) -> None:
        # Called only with controller-owned constant filenames.
        if name not in {"proposals.json", "summary.json"}:
            raise ValueError("Unsupported artifact name")
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
