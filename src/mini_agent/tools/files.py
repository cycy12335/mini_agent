"""Bounded file listing, UTF-8 reads and literal text searches; no writes."""

from functools import wraps
import hashlib
import os
from pathlib import Path

from ..config import AgentConfig
from ..models import ToolError, ToolResult
from ..policy import PolicyGuard


def result_boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ToolError as exc:
            return ToolResult(False, error_code=exc.code, message=str(exc))
        except FileNotFoundError:
            return ToolResult(False, error_code="NOT_FOUND", message="Path does not exist")
        except PermissionError:
            return ToolResult(False, error_code="ACCESS_DENIED", message="Access denied")
        except (OSError, RuntimeError):
            return ToolResult(False, error_code="IO_ERROR", message="Cannot access the requested path")
    return wrapped


class FileTools:
    def __init__(self, policy: PolicyGuard, config: AgentConfig | None = None):
        self.policy = policy
        self.config = config or AgentConfig()
        self.read_paths: set[str] = set()

    def _scan(self, relative: str) -> tuple[list[str], bool, int]:
        start = self.policy.resolve(relative, directory=True)
        pending = [(start, 0)]
        files: list[str] = []
        scanned = skipped = 0
        truncated = False
        while pending:
            directory, depth = pending.pop()
            name = directory.relative_to(self.policy.root).as_posix()
            directory = self.policy.resolve(name, directory=True)
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > self.config.max_scan_entries:
                        return sorted(files), True, skipped
                    name = Path(entry.path).relative_to(self.policy.root).as_posix()
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child = self.policy.resolve(name, directory=True)
                            if depth >= self.config.max_directory_depth:
                                truncated = True
                            else:
                                pending.append((child, depth + 1))
                        else:
                            self.policy.resolve(name)
                            if len(files) >= self.config.max_files:
                                return sorted(files), True, skipped
                            files.append(name)
                    except (ToolError, OSError):
                        skipped += 1
        return sorted(files), truncated, skipped

    def _read(self, relative: str) -> tuple[bytes, str]:
        path = self.policy.resolve(relative)
        if path.stat().st_size > self.config.max_file_bytes:
            raise ToolError("FILE_TOO_LARGE", "File exceeds max_file_bytes; choose a smaller file")
        with path.open("rb") as stream:
            raw = stream.read(self.config.max_file_bytes + 1)
        if len(raw) > self.config.max_file_bytes:
            raise ToolError("FILE_TOO_LARGE", "File exceeds max_file_bytes")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError("NOT_TEXT", "File is not UTF-8 text") from exc
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise ToolError("NOT_TEXT", "Binary or control characters are excluded")
        self.read_paths.add(path.relative_to(self.policy.root).as_posix())
        return raw, text

    @result_boundary
    def list_files(self, path: str = ".") -> ToolResult:
        files, truncated, skipped = self._scan(path)
        selected = []
        size = 0
        for name in files:
            size += len(name)
            if size > self.config.max_tool_output_chars:
                truncated = True
                break
            selected.append(name)
        return ToolResult(True, {"files": selected, "skipped": skipped}, truncated=truncated)

    @result_boundary
    def read_file(self, path: str) -> ToolResult:
        raw, text = self._read(path)
        return ToolResult(True, {"path": path.replace("\\", "/"), "content": text,
                                 "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})

    @result_boundary
    def search_text(self, query: str, path: str = ".") -> ToolResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or "\n" in query or "\r" in query:
            raise ToolError("INVALID_QUERY", "Query must be 1–256 characters on a single line")
        files, truncated, skipped = self._scan(path)
        matches = []
        size = 0
        for name in files:
            try:
                _, text = self._read(name)
            except (ToolError, OSError):
                skipped += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query not in line:
                    continue
                # Bound long matching lines while retaining the actual match.
                match_start = max(0, line.index(query) - 100)
                excerpt = line[match_start:match_start + 500]
                cost = len(name) + len(excerpt)
                if (len(matches) >= self.config.max_search_hits
                        or size + cost > self.config.max_tool_output_chars):
                    return ToolResult(True, {"matches": matches, "skipped": skipped}, truncated=True)
                size += cost
                matches.append({"path": name, "line": number, "text": excerpt,
                                "line_truncated": len(excerpt) != len(line)})
        return ToolResult(True, {"matches": matches, "skipped": skipped}, truncated=truncated)
