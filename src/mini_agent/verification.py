"""Core-owned pytest plans and pre-repair regression copies; no shell commands."""

import ast
import hashlib
from pathlib import Path
import shlex
import json

from .models import ToolError
from .patching import is_test
from .policy import PolicyGuard, _check_links
from .privacy import Redactor
from .repository import discover_repository
from .tools.files import FileTools


def test_nodes(raw):
    tree = ast.parse(raw.decode("utf-8-sig"))
    nodes = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            nodes.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test_"):
                    nodes.add(node.name + "::" + method.name)
    return nodes


class VerificationPlanner:
    def __init__(self, files, config):
        self.files, self.config = files, config
        # Execution was authorized for the repository, including ordinary tests
        # outside the separate model-disclosure scope. Nothing here uploads code.
        self.local = FileTools(files.policy, config)

    def snapshot(self):
        names, truncated, _ = self.local._scan(".")
        hashes, size = {}, 0
        complete = not truncated
        for path in names:
            try:
                raw, _ = self.local._read(path)
                size += len(raw)
                if size > self.config.max_validation_copy_bytes:
                    complete = False
                    break
                hashes[path] = hashlib.sha256(raw).hexdigest()
            except (ToolError, OSError):
                complete = False
        fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()
        return {"hashes": hashes, "complete": complete, "fingerprint": fingerprint}

    def roots(self):
        overview = discover_repository(FileTools(self.files.policy, self.config), local=False)
        configuration = overview["configuration"]
        from .tools.tests import select_pytest_configuration
        selected = select_pytest_configuration(self.files.policy, self.config)
        settings = configuration.get(selected.name, {}).get("pytest", {}) if selected else {}
        roots = settings.get("testpaths", [])
        if isinstance(roots, str):
            roots = shlex.split(roots, posix=False)
            roots = [value.strip('"\'') for value in roots]
        if not isinstance(roots, list) or any(not isinstance(path, str) for path in roots):
            raise ToolError("INVALID_TEST_CONFIGURATION", "pytest testpaths must be literal relative paths")
        roots = [path.replace("\\", "/").removeprefix("./").rstrip("/") for path in roots]
        if not roots:
            try:
                self.files.policy.resolve("tests", directory=True)
                roots = ["tests"]
            except (OSError, ToolError):
                roots = ["."]
        if len(roots) > self.config.max_test_targets:
            raise ToolError("TEST_PLAN_LIMIT", "Configured test root count exceeds limits")
        for path in roots:
            if path.startswith("-") or "::" in path:
                raise ToolError("INVALID_TEST_CONFIGURATION", "pytest testpaths cannot contain options or node selectors")
            try:
                self.files.policy.resolve(path, directory=True)
            except ToolError as exc:
                if exc.code != "NOT_DIRECTORY":
                    raise
                self.files.policy.resolve(path)
            except OSError as exc:
                raise ToolError("TEST_ROOT_UNAVAILABLE", "A configured pytest test root is missing or unreadable") from exc
        return list(dict.fromkeys(roots)), overview

    def proposal(self, proposal, baseline, index, revision):
        roots, overview = self.roots()
        regressions = []
        for edit in proposal.edits:
            if is_test(edit.path):
                previous = set() if edit.operation == "create" else test_nodes(edit.original)
                for node in sorted(test_nodes(edit.replacement) - previous):
                    regressions.append(edit.path + "::" + node)
        if len(regressions) > self.config.max_test_targets:
            raise ToolError("TEST_PLAN_LIMIT", "New regression selector count exceeds limits")
        files = set(overview["test_files"]) | {edit.path for edit in proposal.edits if is_test(edit.path)}
        def covered(name):
            return any(root == "." or name == root or name.startswith(root.rstrip("/") + "/") for root in roots)
        suite_files = {name for name in files if covered(name)}
        targets = {case.split("::", 1)[0] for case, outcome in (baseline.cases or {}).items()
                   if outcome in {"failed", "error"}} if baseline else set()
        targets.update(selector.split("::", 1)[0] for selector in regressions)
        affected = set()
        # Static candidates are helpful selectors, never proof of full coverage.
        try:
            from .symbols import SymbolIndex
            symbols = SymbolIndex(self.files)
            symbols.refresh()
            changed_paths = {edit.path for edit in proposal.edits if not is_test(edit.path)}
            identities = {definition["id"] for definition in symbols.definitions if definition["path"] in changed_paths}
            affected.update(item["path"] for item in symbols.related_tests(identities))
        except (OSError, ToolError, SyntaxError):
            pass  # Full configured suite remains mandatory; do not claim complete impact analysis.
        if not targets:
            targets.update(affected)
        steps = []
        # Small projects need one full invocation, not repeated identical coverage.
        if suite_files and targets >= suite_files and not overview["truncated"]:
            steps.append({"phase": "post_edit", "targets": roots, "covers": ["target", "affected", "configured_suite"], "full": True})
        else:
            if targets:
                steps.append({"phase": "post_target", "targets": sorted(targets), "covers": ["target"], "full": False})
            if affected - targets:
                steps.append({"phase": "post_affected", "targets": sorted(affected - targets), "covers": ["affected"], "full": False})
            steps.append({"phase": "post_edit", "targets": roots, "covers": ["configured_suite"], "full": True})
        for step in steps:
            if len(step["targets"]) > self.config.max_test_targets:
                raise ToolError("TEST_PLAN_LIMIT", "Planned test selector count exceeds limits")
        return {"version": 1, "id": f"V{index:04}", "proposal": index, "source_revision": revision,
                "status": "proposed", "regressions": regressions, "pre_repair": None,
                "steps": steps, "results": [], "scope": "configured_pytest_roots",
                "notice": "Verified covers executed configured tests, not all possible repository behavior."}


class RegressionWorkspace:
    def __init__(self, planner, proposal, directory, *, cancelled=lambda: False):
        self.directory = directory
        self.hashes = {}
        _check_links(directory.parent)
        directory.mkdir(exist_ok=False)
        names, truncated, _ = planner.local._scan(".")
        if truncated:
            raise ToolError("REGRESSION_COPY_LIMIT", "Pre-repair inventory is incomplete")
        redactor = getattr(planner.files, "redactor", None) or Redactor()
        size = 0
        for name in names:
            if cancelled():
                raise ToolError("CANCELLED", "Pre-repair copy cancelled")
            raw, text = planner.local._read(name)
            if redactor.text(text) != text:
                raise ToolError("REGRESSION_COPY_INCOMPLETE", "Sensitive content prevents a complete pre-repair copy")
            size += len(raw)
            if size > planner.config.max_validation_copy_bytes:
                raise ToolError("REGRESSION_COPY_LIMIT", "Pre-repair copy exceeds byte budget")
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            self.hashes[name] = hashlib.sha256(raw).hexdigest()
        for edit in proposal.edits:
            if is_test(edit.path):
                path = directory / edit.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(edit.replacement)
        self.expected_copy = dict(self.hashes)
        self.expected_copy.update({edit.path: hashlib.sha256(edit.replacement).hexdigest() for edit in proposal.edits if is_test(edit.path)})
        _check_links(directory)
        # The workspace is core-owned audit data, never a model-selectable target.
        self.policy = PolicyGuard.__new__(PolicyGuard)
        self.policy.root = directory.resolve(strict=True)

    def validate_origin(self, planner):
        copy_reader = FileTools(self.policy, planner.config)
        for path, digest in self.expected_copy.items():
            try:
                raw, _ = copy_reader._read(path)
            except (OSError, ToolError) as exc:
                raise ToolError("REGRESSION_COPY_MUTATED", "Reproduction modified or removed copied source/test files") from exc
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ToolError("REGRESSION_COPY_MUTATED", "Reproduction modified copied source/test files")
        names, truncated, _ = planner.local._scan(".")
        if truncated or set(names) != set(self.hashes):
            raise ToolError("HASH_CONFLICT", "Repository changed during pre-repair reproduction")
        for path, expected in self.hashes.items():
            raw, _ = planner.local._read(path)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ToolError("HASH_CONFLICT", "Repository changed during pre-repair reproduction")


def failed_regressions(result, selectors):
    if (not result.complete or result.cancelled or result.timed_out or result.error
            or not result.cleanup_ok or result.collection_errors):
        raise ToolError("REGRESSION_EXECUTION_ERROR", "Incomplete regression execution does not establish reproduction")
    matching = {case: outcome for case, outcome in (result.cases or {}).items()
                if any(case == node or case.startswith(node + "[") for node in selectors)}
    if any(outcome == "error" for outcome in matching.values()):
        raise ToolError("REGRESSION_EXECUTION_ERROR", "Regression setup/teardown errors do not establish reproduction")
    failed = [case for case, outcome in matching.items() if outcome == "failed"]
    failures = result.failure_types or {}
    if any(case not in failures for case in failed):
        raise ToolError("REGRESSION_EXECUTION_ERROR", "Regression exception type is unknown; reproduction is unconfirmed")
    if any(failures.get(case) in {"ModuleNotFoundError", "ImportError", "FileNotFoundError", "PermissionError"} for case in failed):
        raise ToolError("REGRESSION_ENVIRONMENT_INCOMPLETE", "Missing dependency/file or permission errors in a copy are not bug reproduction")
    if not failed:
        raise ToolError("REGRESSION_NOT_REPRODUCED", "New regression cases did not fail before repair; improve the test and submit a fresh proposal")
    return sorted(failed)
