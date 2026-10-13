"""Structured results shared by the read-only tools."""

from dataclasses import asdict, dataclass
from typing import Any


class ToolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any = None
    error_code: str | None = None
    message: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
