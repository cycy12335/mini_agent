"""Guarded proposal dispatch; only the controller can approve/apply edits."""

from pathlib import Path

from ..actions import validate_action
from ..models import ToolError, ToolResult
from .files import FileTools, result_boundary


class ToolRegistry:
    def __init__(self, files: FileTools):
        self.files = files
        self.read_files = files.read_paths
        self.proposals: list[list[dict]] = []

    def execute(self, action: dict) -> ToolResult:
        # Validate again so direct callers cannot bypass the controller boundary.
        action = validate_action(action, self.files.config)
        if action["type"] != "tool_call":
            raise ValueError("Only tool actions can be dispatched")
        name, args = action["name"], action["arguments"]
        if name == "list_files":
            return self.files.list_files(**args)
        if name == "read_file":
            result = self.files.read_file(**args)
            if result.ok:
                self.read_files.add(result.data["path"])
            return result
        if name == "search_text":
            return self.files.search_text(**args)
        if name == "propose_edits":
            return self._propose(args["edits"])
        return ToolResult(False, error_code="TESTS_UNAVAILABLE",
                          message="Test execution requires the authorized controller")

    @result_boundary
    def _propose(self, edits: list[dict]) -> ToolResult:
        checked = []
        seen: set[Path] = set()
        for edit in edits:
            path = self.files.policy.resolve_edit(edit["path"])
            if path in seen:
                raise ToolError("DUPLICATE_PATH", "A file can appear only once per proposal")
            seen.add(path)
            # V1 edits are restricted to Python source/tests, never config/manifests.
            current = self.files.read_file(edit["path"])
            if not current.ok:
                return current
            if current.data["sha256"] != edit["original_sha256"]:
                raise ToolError("HASH_CONFLICT", "Original content changed; read the file again")
            self.read_files.add(current.data["path"])
            checked.append({**edit, "path": path.relative_to(self.files.policy.root).as_posix()})
        self.proposals.append(checked)
        return ToolResult(True, {"proposal": len(self.proposals), "files": [e["path"] for e in checked],
                                 "applied": False}, message="Proposal recorded; controller approval is required for writing")
