"""Approved single-file replacements, transaction backups and conflict-safe recovery."""

import ast
from dataclasses import dataclass, replace
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .actions import validate_action
from .config import AgentConfig
from .models import ToolError
from .policy import PolicyGuard, _check_links
from .tools.files import FileTools


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int | None = None, before_replace=None) -> None:
    """Flush a same-directory temporary file, then replace one directory entry."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".mini-agent-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def is_test(path: str) -> bool:
    p = Path(path.lower())
    return "tests" in p.parts or p.name.startswith("test_") or p.name.endswith("_test.py")


def check_test_change(path: str, old: bytes, new: bytes) -> None:
    if not is_test(path) or old == new:
        return
    # Conservatively support added tests, never rewriting existing assertions.
    try:
        previous = ast.parse(old.decode("utf-8-sig"))
        updated = ast.parse(new.decode("utf-8-sig"))
    except (SyntaxError, UnicodeError) as exc:
        raise ToolError("TEST_EDIT_FORBIDDEN", "Test changes must remain valid Python") from exc
    old_nodes = [ast.dump(node) for node in previous.body]
    if [ast.dump(node) for node in updated.body[:len(old_nodes)]] != old_nodes:
        raise ToolError("TEST_EDIT_FORBIDDEN", "Existing test code/assertions cannot be changed or removed")
    names = {n.id for n in ast.walk(previous) if isinstance(n, ast.Name)}
    names.update(n.name for n in ast.walk(previous) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    for node in updated.body[len(old_nodes):]:
        if (not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_")
                or node.name in names or node.decorator_list or node.args.defaults
                or node.args.kw_defaults and any(v is not None for v in node.args.kw_defaults)):
            raise ToolError("TEST_EDIT_FORBIDDEN", "Only new, undecorated test functions can be appended")
        names.add(node.name)


@dataclass(frozen=True)
class PreparedEdit:
    path: str
    original: bytes
    replacement: bytes
    mode: int


@dataclass(frozen=True)
class PreparedProposal:
    edits: tuple[PreparedEdit, ...]
    diff: str


class PatchManager:
    def __init__(self, policy: PolicyGuard, directory: Path, config: AgentConfig | None = None,
                 *, read_only: bool = True):
        self.policy, self.directory = policy, directory.absolute()
        self.config = config or AgentConfig()
        self.files = FileTools(policy, self.config)
        self.read_only = read_only
        _check_links(self.directory)
        if self.directory.resolve().is_relative_to(policy.root):
            raise ToolError("INVALID_ARTIFACT_PATH", "Backups must be outside the target")
        self.manifest_path = self.directory / "manifest.json"
        self.manifest = {"version": 1, "project": str(policy.root), "max_file_bytes": self.config.max_file_bytes,
                         "transactions": []}
        if self.manifest_path.exists():
            self.manifest = self._load_manifest()

    def _artifact(self, path: Path) -> Path:
        if not path.is_relative_to(self.directory):
            raise ToolError("INVALID_ARTIFACT_PATH", "Artifact escapes run directory")
        _check_links(path.parent)
        if path.exists() or path.is_symlink():
            _check_links(path)
            if path.is_file() and path.stat().st_nlink > 1:
                raise ToolError("INVALID_ARTIFACT_PATH", "Hard-linked artifacts are forbidden")
        return path

    def _save(self) -> None:
        atomic_write(self._artifact(self.manifest_path),
                     json.dumps(self.manifest, indent=2).encode("utf-8"))

    def _load_manifest(self) -> dict:
        path = self._artifact(self.manifest_path)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ToolError("INVALID_MANIFEST", "Manifest is too large")
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        self._manifest_hash = digest(raw)
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("project") != str(self.policy.root) or not isinstance(data.get("transactions"), list)):
            raise ToolError("INVALID_MANIFEST", "Invalid project or manifest format")
        if type(data.get("max_file_bytes")) is not int or data["max_file_bytes"] <= 0:
            raise ToolError("INVALID_MANIFEST", "Invalid stored file limit")
        self.config = replace(self.config, max_file_bytes=data["max_file_bytes"])
        self.files = FileTools(self.policy, self.config)
        for tx in data["transactions"]:
            if (not isinstance(tx, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(tx.get("id", "")))
                    or tx.get("status") not in {"prepared", "applied", "recovered", "recovery_conflict", "rolled_back", "rollback_failed"}
                    or not isinstance(tx.get("files"), list) or not tx["files"]):
                raise ToolError("INVALID_MANIFEST", "Invalid transaction")
            seen = set()
            for index, entry in enumerate(tx["files"]):
                if not isinstance(entry, dict):
                    raise ToolError("INVALID_MANIFEST", "Invalid file entry")
                self.policy.resolve_edit(entry.get("path"))
                if entry["path"] in seen or entry.get("backup") != f"backups/{tx['id']}-{index}.bin":
                    raise ToolError("INVALID_MANIFEST", "Invalid backup or duplicate path")
                seen.add(entry["path"])
                if not all(isinstance(entry.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", entry[k])
                           for k in ("original_sha256", "new_sha256")):
                    raise ToolError("INVALID_MANIFEST", "Invalid content hash")
                if type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777:
                    raise ToolError("INVALID_MANIFEST", "Invalid file permissions")
                if entry.get("state") not in {"pending", "writing", "written", "restored"}:
                    raise ToolError("INVALID_MANIFEST", "Invalid file write state")
        return data

    def _current(self, path: str) -> tuple[Path, bytes]:
        resolved = self.policy.resolve_edit(path)
        raw, _ = self.files._read(path)
        return resolved, raw

    def _assert_hash(self, path: str, expected: str) -> None:
        _, raw = self._current(path)
        if digest(raw) != expected:
            raise ToolError("HASH_CONFLICT", f"Content changed: {path}; approval is no longer valid")

    def preview(self, edits: list[dict]) -> PreparedProposal:
        validate_action({"type": "tool_call", "name": "propose_edits", "arguments": {"edits": edits}}, self.config)
        prepared, diffs, seen = [], [], set()
        for edit in edits:
            path, raw = self._current(edit["path"])
            if path in seen:
                raise ToolError("DUPLICATE_PATH", "Duplicate proposal path")
            seen.add(path)
            if digest(raw) != edit["original_sha256"]:
                raise ToolError("HASH_CONFLICT", f"Content changed: {edit['path']}")
            replacement = edit["content"].encode("utf-8")
            if any(ord(c) < 32 and c not in "\n\r\t" for c in edit["content"]):
                raise ToolError("NOT_TEXT", "Replacement must be text without control characters")
            relative = path.relative_to(self.policy.root).as_posix()
            check_test_change(relative, raw, replacement)
            if raw == replacement:
                continue
            prepared.append(PreparedEdit(relative, raw, replacement, stat.S_IMODE(path.stat().st_mode) & 0o777))
            if is_test(relative):
                diffs.append(f"# TEST CHANGE: {relative}\n")
            # Keep raw UTF-8 BOM and line-ending changes visible in the stored diff.
            diff = difflib.unified_diff(raw.decode("utf-8").splitlines(keepends=True),
                                       replacement.decode("utf-8").splitlines(keepends=True),
                                       fromfile=f"a/{relative}", tofile=f"b/{relative}")
            for line in diff:
                diffs.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        if not prepared:
            raise ToolError("NO_CHANGES", "Proposal contains no changes")
        proposal = PreparedProposal(tuple(prepared), "".join(diffs))
        return proposal

    def _replace_file(self, relative: str, content: bytes, mode: int, expected_hash: str) -> None:
        path, _ = self._current(relative)
        atomic_write(path, content, mode, lambda: self._assert_hash(relative, expected_hash))

    def apply(self, proposal: PreparedProposal, approval: bool) -> list[str]:
        if self.read_only:
            raise ToolError("READ_ONLY", "This session cannot modify the target")
        if approval is not True:
            raise ToolError("APPROVAL_REQUIRED", "Explicit approval is required")
        # Rebuild from frozen content, so application cannot skip policy or hash validation.
        checked = self.preview([{"path": e.path, "original_sha256": digest(e.original),
                                 "content": e.replacement.decode("utf-8")} for e in proposal.edits])
        if checked != proposal:
            raise ToolError("PROPOSAL_CHANGED", "Preview changed; a new approval is required")
        tx = {"id": uuid.uuid4().hex, "status": "prepared", "files": []}
        backup_dir = self.directory / "backups"
        self._artifact(backup_dir)
        backup_dir.mkdir(exist_ok=True)
        for index, edit in enumerate(proposal.edits):
            backup = f"backups/{tx['id']}-{index}.bin"
            atomic_write(self._artifact(self.directory / backup), edit.original)
            tx["files"].append({"path": edit.path, "backup": backup, "mode": edit.mode,
                                "state": "pending", "original_sha256": digest(edit.original),
                                "new_sha256": digest(edit.replacement)})
        self.manifest["transactions"].append(tx)
        self._save()  # Write-ahead journal and all backups exist before touching source.
        try:
            for edit, entry in zip(proposal.edits, tx["files"]):
                self._assert_hash(edit.path, digest(edit.original))
                entry["state"] = "writing"
                self._save()
                try:
                    self._replace_file(edit.path, edit.replacement, edit.mode, digest(edit.original))
                except ToolError:
                    # Guard failures occur before os.replace: this file was not written.
                    entry["state"] = "pending"
                    raise
                entry["state"] = "written"
                self._save()
            tx["status"] = "applied"
            self._save()
        except BaseException:
            conflicts = self._recover_transaction(tx)
            tx["status"] = "recovery_conflict" if conflicts else "recovered"
            tx["conflicts"] = conflicts
            self._save()
            if conflicts:
                raise ToolError("RECOVERY_CONFLICT", "Some files need manual recovery; see manifest.json")
            raise
        return [edit.path for edit in proposal.edits]

    def _backup(self, entry: dict) -> bytes:
        path = self._artifact(self.directory / entry["backup"])
        with path.open("rb") as stream:
            data = stream.read(self.config.max_file_bytes + 1)
        if len(data) > self.config.max_file_bytes or digest(data) != entry["original_sha256"]:
            raise ToolError("BACKUP_CORRUPT", "Backup failed size/hash validation")
        return data

    def _recover_transaction(self, tx: dict) -> list[str]:
        conflicts = []
        for entry in reversed(tx["files"]):
            if entry["state"] in {"pending", "restored"}:
                continue
            try:
                original = self._backup(entry)
                _, current = self._current(entry["path"])
                if digest(current) == entry["original_sha256"]:
                    entry["state"] = "restored"
                    continue
                if digest(current) != entry["new_sha256"]:
                    conflicts.append(entry["path"])
                    continue
                self._replace_file(entry["path"], original, entry["mode"], entry["new_sha256"])
                entry["state"] = "restored"
            except (OSError, ValueError):
                conflicts.append(entry["path"])
        return conflicts

    def rollback_preview(self) -> list[str]:
        self.manifest = self._load_manifest()
        virtual, names = {}, []
        for tx in reversed(self.manifest["transactions"]):
            if tx["status"] in {"recovered", "rolled_back"}:
                continue
            for entry in reversed(tx["files"]):
                if entry["state"] in {"pending", "restored"}:
                    continue
                self._backup(entry)
                name = entry["path"]
                if name not in virtual:
                    virtual[name] = digest(self._current(name)[1])
                if virtual[name] not in {entry["new_sha256"], entry["original_sha256"]}:
                    raise ToolError("HASH_CONFLICT", f"Later changes in {name}; rollback refused")
                virtual[name] = entry["original_sha256"]
                if name not in names:
                    names.append(name)
        self._rollback_approval_hash = self._manifest_hash
        return names

    def rollback(self, approval: bool) -> list[str]:
        if self.read_only or approval is not True:
            raise ToolError("APPROVAL_REQUIRED", "Rollback requires a writable session and explicit approval")
        approved_manifest = getattr(self, "_rollback_approval_hash", None)
        if approved_manifest is None:
            raise ToolError("APPROVAL_REQUIRED", "Preview the recovery list before approval")
        names = self.rollback_preview()  # Recheck all files after confirmation, before any write.
        if self._manifest_hash != approved_manifest:
            del self._rollback_approval_hash
            raise ToolError("PROPOSAL_CHANGED", "Recovery manifest changed; preview and approve again")
        del self._rollback_approval_hash
        for tx in reversed(self.manifest["transactions"]):
            if tx["status"] in {"recovered", "rolled_back"}:
                continue
            conflicts = self._recover_transaction(tx)
            tx["status"] = "rollback_failed" if conflicts else "rolled_back"
            tx["conflicts"] = conflicts
            self._save()
            if conflicts:
                raise ToolError("RECOVERY_CONFLICT", "Recovery incomplete; see manifest.json and retry after resolving conflicts")
        return names

    def modified_files(self) -> list[str]:
        return sorted({e["path"] for tx in self.manifest["transactions"]
                       if tx["status"] in {"applied", "prepared", "recovery_conflict", "rollback_failed"}
                       for e in tx["files"] if e["state"] in {"writing", "written"}})
