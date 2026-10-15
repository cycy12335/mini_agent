"""Bounded preflight for proposed Python syntax and local import consistency."""

import ast
import hashlib
import time

from .models import ToolError
from .privacy import ScopedFiles
from .repository import discover_repository
from .tools.files import FileTools


def exports(tree):
    found = set()
    dynamic = False
    def visit(node):
        nonlocal dynamic
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
            dynamic |= node.name == "__getattr__"
            return
        if isinstance(node, ast.Import):
            found.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
            dynamic |= any(alias.name == "*" for alias in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval"}:
            dynamic = True
        for child in ast.iter_child_nodes(node):
            visit(child)
    visit(tree)
    return found, dynamic


class ConsistencyChecker:
    def __init__(self, files):
        self.files = files
        self.cancelled = lambda: False
        self.deadline = float("inf")

    def check_budget(self):
        if self.cancelled():
            raise ToolError("CANCELLED", "Repair preflight cancelled")
        if time.monotonic() >= self.deadline:
            raise ToolError("TIME_LIMIT", "Repair preflight exceeded the run time budget")

    def check(self, edits):
        self.check_budget()
        reader = ScopedFiles(self.files.policy, self.files.config, self.files.allowed, self.files.redactor) if isinstance(self.files, ScopedFiles) else FileTools(self.files.policy, self.files.config)
        overview = discover_repository(reader, local=False)
        modules = {item["path"]: item["module"] for item in overview["modules"]}
        names, truncated, _ = reader._scan(".")
        if truncated or overview["truncated"]:
            raise ToolError("CONSISTENCY_LIMIT", "Repository inventory is incomplete; increase budgets or narrow the approved scope")
        proposed = {edit.path: edit for edit in edits}
        trees, dependencies, warnings = {}, {}, []
        size = nodes = 0
        for name in sorted(set(names) | set(proposed)):
            self.check_budget()
            if not name.endswith((".py", ".pyi")):
                continue
            if name in proposed:
                raw = proposed[name].replacement
            else:
                try:
                    raw, _ = reader._read(name)
                    dependencies[name] = hashlib.sha256(raw).hexdigest()
                except (OSError, ToolError):
                    warnings.append({"path": name, "code": "UNREADABLE_DEPENDENCY"})
                    continue
            size += len(raw)
            if size > reader.config.max_index_bytes:
                raise ToolError("CONSISTENCY_LIMIT", "Consistency source budget exhausted")
            try:
                tree = ast.parse(raw.decode("utf-8-sig"), filename=name)
                for _ in ast.walk(tree):
                    nodes += 1
                    if nodes > reader.config.max_index_nodes:
                        raise ToolError("CONSISTENCY_LIMIT", "Consistency AST budget exhausted")
            except (SyntaxError, UnicodeError, RecursionError):
                if name in proposed:
                    raise ToolError("INVALID_PYTHON", f"Replacement is not valid Python: {name}")
                warnings.append({"path": name, "code": "SYNTAX_ERROR"})
                continue
            trees[name] = tree
            if name not in modules:
                parent = name.rsplit("/", 1)[0] if "/" in name else ""
                neighbor = next((path for path in modules if (path.rsplit("/", 1)[0] if "/" in path else "") == parent), None)
                if neighbor:
                    prefix = modules[neighbor] if neighbor.endswith("/__init__.py") else modules[neighbor].rsplit(".", 1)[0] if "." in modules[neighbor] else ""
                    modules[name] = ".".join(value for value in (prefix, name.rsplit("/", 1)[-1].rsplit(".", 1)[0]) if value).removesuffix(".__init__")
                else:
                    modules[name] = name.removeprefix("src/").rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")
        # Config declarations influence module resolution too.
        for name in overview["configuration"]:
            raw, _ = reader._read(name)
            dependencies[name] = hashlib.sha256(raw).hexdigest()
        available = {}
        changed = set()
        for name, tree in trees.items():
            self.check_budget()
            aliases = {modules[name], name.rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")}
            for module in aliases:
                available.setdefault(module, []).append(exports(tree))
                if name in proposed:
                    changed.add(module)
        for name, tree in trees.items():
            module = modules[name]
            package = module.split(".") if name.endswith("/__init__.py") else module.split(".")[:-1]
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.level:
                    if node.level > len(package):
                        if name in proposed:
                            raise ToolError("IMPORT_INCONSISTENCY", f"Invalid relative import in {name}:{node.lineno}")
                        continue
                    base = ".".join(package[:len(package) - node.level + 1])
                    target = ".".join(value for value in (base, node.module) if value)
                else:
                    target = node.module or ""
                if target not in changed or len(available.get(target, [])) != 1:
                    continue
                symbols, dynamic = available[target][0]
                for alias in node.names:
                    if alias.name != "*" and not dynamic and alias.name not in symbols and target + "." + alias.name not in available:
                        raise ToolError("IMPORT_INCONSISTENCY", f"Proposed local export {target}.{alias.name} is missing; referenced by {name}:{node.lineno}")
        inventory = tuple(name for name in names if name.endswith((".py", ".pyi")))
        return tuple(sorted(dependencies.items())), tuple(warnings), inventory
