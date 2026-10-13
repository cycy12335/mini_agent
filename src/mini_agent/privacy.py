"""Credential scrubbing and a frozen, user-reviewed file disclosure scope."""

import re

from .models import ToolError
from .tools.files import FileTools


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})", "[REDACTED]", value)
        value = re.sub(r"(?im)(\b(?:api[_-]?key|access[_-]?token|password|secret)\b\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", value)
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED]", value, flags=re.S)
        return value

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.text(k): self.value(v) for k, v in value.items()}
        return value


class ScopedFiles(FileTools):
    """New files and files outside the approved manifest require a new run/approval."""

    def __init__(self, policy, config, allowed, redactor):
        super().__init__(policy, config)
        self.allowed = frozenset(allowed)
        self.redactor = redactor

    def _scan(self, relative):
        files, truncated, skipped = super()._scan(relative)
        return [name for name in files if name in self.allowed], truncated, skipped

    def _read(self, relative):
        name = self.policy.resolve(relative).relative_to(self.policy.root).as_posix()
        if name not in self.allowed:
            raise ToolError("SEND_SCOPE_DENIED", "File is outside the approved disclosure scope; start a new run to expand it")
        raw, text = super()._read(name)
        if self.redactor.text(text) != text:
            raise ToolError("SENSITIVE_CONTENT", "File contains credential-like content; automatic disclosure/editing is blocked")
        return raw, text


def disclosure_files(files, paths):
    selected = set()
    for path in paths or ["."]:
        try:
            files.policy.resolve(path, directory=True)
        except ToolError:
            resolved = files.policy.resolve(path)
            selected.add(resolved.relative_to(files.policy.root).as_posix())
        else:
            names, truncated, _ = files._scan(path)
            if truncated:
                raise ToolError("SEND_SCOPE_TOO_LARGE", "Disclosure inventory is truncated; narrow --send-path")
            selected.update(names)
    if len(selected) > files.config.max_files or sum(map(len, selected)) > files.config.max_tool_output_chars:
        raise ToolError("SEND_SCOPE_TOO_LARGE", "Disclosure inventory exceeds limits; narrow --send-path")
    if not selected:
        raise ToolError("EMPTY_SEND_SCOPE", "No eligible files in the selected scope")
    return sorted(selected)
