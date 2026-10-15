"""Scoped Python AST evidence. Static candidates are not runtime call guarantees."""

import ast
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import time

from .models import ToolError, ToolResult
from .privacy import Redactor, ScopedFiles
from .repository import discover_repository
from .tools.files import FileTools, result_boundary


@dataclass
class Scope:
    parent: "Scope | None"
    name: str
    kind: str
    owner: str
    bindings: dict = field(default_factory=dict)
    globals: set = field(default_factory=set)
    nonlocals: set = field(default_factory=set)
    wildcard: bool = False

    def bind(self, name, value):
        self.bindings.setdefault(name, []).append(value)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        if base:
            return base + "." + node.attr
    return None


class FileIndex(ast.NodeVisitor):
    def __init__(self, path, module, text, digest, tree):
        self.path, self.module, self.sha256 = path, module, digest
        self.definitions, self.references, self.imports = [], [], []
        self.conditional = 0
        self.scope = self.root = Scope(None, "", "module", path + "::<module>")
        self.scopes = [self.root]
        self.visit(tree)
        for scope in self.scopes:
            for name in scope.globals & scope.bindings.keys():
                self.root.bind(name, ("unknown", "global_write"))
            for name in scope.nonlocals & scope.bindings.keys():
                parent = scope.parent
                while parent and (parent.kind != "function" or name not in parent.bindings):
                    parent = parent.parent
                if parent:
                    parent.bind(name, ("unknown", "nonlocal_write"))

    def location(self, node):
        return {"path": self.path, "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "column": node.col_offset, "sha256": self.sha256}

    def definition(self, node, kind):
        qualname = ".".join(value for value in (self.scope.name, node.name) if value)
        identity = f"{self.path}::{qualname}@{node.lineno}"
        definition = {**self.location(node), "id": identity, "name": node.name,
                      "qualname": qualname, "symbol": self.module + "." + qualname,
                      "kind": kind, "parent": self.scope.owner,
                      "decorated": bool(node.decorator_list),
                      "conditional": bool(self.conditional),
                      "is_test": Path(self.path).name.startswith("test_") or self.path.endswith("_test.py")}
        self.definitions.append(definition)
        self.scope.bind(node.name, ("definition", identity))
        return definition

    def visit_FunctionDef(self, node):
        definition = self.definition(node, "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
        # Defaults, decorators and annotations execute in the enclosing scope.
        for value in node.decorator_list + node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        args += [v for v in (node.args.vararg, node.args.kwarg) if v]
        for arg in args:
            if arg.annotation:
                self.visit(arg.annotation)
        if node.returns:
            self.visit(node.returns)
        enclosing = self.scope
        conditional = self.conditional
        self.conditional = 0
        self.scope = Scope(enclosing, definition["qualname"], "function", definition["id"])
        self.scopes.append(self.scope)
        for arg in args:
            self.scope.bind(arg.arg, ("unknown", "parameter"))
        for statement in node.body:
            self.visit(statement)
        self.scope = enclosing
        self.conditional = conditional

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        definition = self.definition(node, "class")
        for value in node.decorator_list + node.bases + [v.value for v in node.keywords]:
            self.visit(value)
        enclosing = self.scope
        self.scope = Scope(enclosing, definition["qualname"], "class", definition["id"])
        self.scopes.append(self.scope)
        for statement in node.body:
            self.visit(statement)
        self.scope = enclosing

    def visit_Import(self, node):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            canonical = alias.name if alias.asname else local
            self.add_import(node, local, canonical)

    def visit_ImportFrom(self, node):
        package = self.module.split(".") if self.path.endswith("/__init__.py") else self.module.split(".")[:-1]
        if node.level:
            valid = node.level <= len(package)
            base = ".".join(package[:len(package) - node.level + 1]) if valid else "<invalid-relative-import>"
            base = ".".join(value for value in (base, node.module) if value)
        else:
            base = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self.scope.wildcard = True
                self.imports.append({**self.location(node), "local": "*", "target": base,
                                     "uncertain": True, "reason": "wildcard_import"})
            else:
                self.add_import(node, alias.asname or alias.name, base + "." + alias.name)

    def add_import(self, node, local, canonical):
        uncertain = self.scope.kind != "module" or bool(self.conditional)
        self.scope.bind(local, ("runtime_import" if uncertain else "import", canonical))
        item = {**self.location(node), "local": local, "target": canonical,
                "uncertain": uncertain, "reason": "runtime_import" if uncertain else "static_import"}
        self.imports.append(item)
        self.references.append({**self.location(node), "expression": local, "owner": self.scope.owner,
                                "scope": self.scope, "kind": "import", "canonical": canonical,
                                "runtime_import": item["uncertain"]})

    def reference(self, node, kind="reference"):
        expression = dotted(node)
        self.references.append({**self.location(node), "expression": expression or "<dynamic-expression>",
                                "owner": self.scope.owner, "scope": self.scope, "kind": kind})

    def visit_Call(self, node):
        self.reference(node.func, "call")
        # Record the callee once, but still inspect a dynamic expression's children.
        if isinstance(node.func, ast.Attribute):
            self.visit(node.func.value)
        elif not isinstance(node.func, ast.Name):
            self.visit(node.func)
        for value in node.args + [keyword.value for keyword in node.keywords]:
            self.visit(value)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.reference(node)
        else:
            self.scope.bind(node.id, ("unknown", "assignment_or_deletion"))

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        enclosing = self.scope
        while self.scope.kind in {"comprehension", "generator"}:
            self.scope = self.scope.parent
        self.visit(node.target)
        self.scope = enclosing

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.reference(node)
        self.visit(node.value)

    def visit_Global(self, node):
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node):
        self.scope.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.scope.bind(node.name, ("unknown", "exception_binding"))
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.scope.bind(node.name, ("unknown", "pattern_binding"))
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.scope.bind(node.rest, ("unknown", "pattern_binding"))
        self.generic_visit(node)

    def visit_Lambda(self, node):
        enclosing = self.scope
        for value in node.args.defaults + [v for v in node.args.kw_defaults if v]:
            self.visit(value)
        self.scope = Scope(enclosing, enclosing.name, "lambda", enclosing.owner)
        self.scopes.append(self.scope)
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [v for v in (node.args.vararg, node.args.kwarg) if v]:
            self.scope.bind(arg.arg, ("unknown", "lambda_parameter"))
        self.visit(node.body)
        self.scope = enclosing

    def comprehension(self, node):
        # The first iterable belongs to the outer scope; targets are local.
        self.visit(node.generators[0].iter)
        enclosing = self.scope
        self.scope = Scope(enclosing, enclosing.name,
                           "generator" if isinstance(node, ast.GeneratorExp) else "comprehension", enclosing.owner)
        self.scopes.append(self.scope)
        for index, generator in enumerate(node.generators):
            self.visit(generator.target)
            if index:
                self.visit(generator.iter)
            for value in generator.ifs:
                self.visit(value)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scope = enclosing

    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension
    visit_GeneratorExp = comprehension

    def conditional_block(self, node):
        self.conditional += 1
        self.generic_visit(node)
        self.conditional -= 1

    visit_If = conditional_block
    visit_For = conditional_block
    visit_AsyncFor = conditional_block
    visit_While = conditional_block
    visit_Try = conditional_block
    visit_TryStar = conditional_block
    visit_Match = conditional_block


class SymbolIndex:
    def __init__(self, files):
        self.files = files
        # Keep full inventory reads separate from evidence actually returned.
        self.reader = ScopedFiles(files.policy, files.config, files.allowed, files.redactor) if isinstance(files, ScopedFiles) else FileTools(files.policy, files.config)
        self.cache = {}
        self.parsed_files = 0
        self.cancelled = lambda: False
        self.deadline = float("inf")

    def check_budget(self):
        if self.cancelled():
            raise ToolError("CANCELLED", "Symbol indexing cancelled")
        if time.monotonic() >= self.deadline:
            raise ToolError("TIME_LIMIT", "Symbol indexing exhausted the run time budget")

    def refresh(self):
        self.check_budget()
        overview = discover_repository(self.reader, local=False)
        module_names = {item["path"]: item["module"] for item in overview["modules"]}
        names, scan_truncated, skipped = self.reader._scan(".")
        active = {}
        errors = []
        consumed = nodes = 0
        limited = overview["truncated"] or scan_truncated
        redactor = getattr(self.reader, "redactor", None) or Redactor()
        for name in names:
            if not name.endswith((".py", ".pyi")):
                continue
            self.check_budget()
            try:
                raw, text = self.reader._read(name)
                if redactor.text(text) != text:
                    raise ToolError("SENSITIVE_CONTENT", "Sensitive content is excluded from indexes")
                consumed += len(raw)
                if consumed > self.files.config.max_index_bytes:
                    limited = True
                    break
                module = module_names.get(name, name.removeprefix("src/").rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__"))
                digest = hashlib.sha256(raw).hexdigest()
                previous = self.cache.get(name)
                if previous and previous[0] == digest and previous[1].module == module:
                    count, index = previous[2], previous[1]
                else:
                    tree = ast.parse(text, filename=name)
                    count = 0
                    for _ in ast.walk(tree):
                        count += 1
                        if count + nodes > self.files.config.max_index_nodes:
                            break
                    if count + nodes > self.files.config.max_index_nodes:
                        limited = True
                        break
                    index = FileIndex(name, module, text, digest, tree)
                    self.parsed_files += 1
                if count + nodes > self.files.config.max_index_nodes:
                    limited = True
                    break
                nodes += count
                active[name] = (digest, index, count)
            except SyntaxError as exc:
                errors.append({"path": name, "line": exc.lineno, "code": "SYNTAX_ERROR"})
            except (ToolError, OSError, RecursionError, ValueError) as exc:
                if isinstance(exc, ToolError) and exc.code in {"CANCELLED", "TIME_LIMIT"}:
                    raise
                errors.append({"path": name, "code": getattr(exc, "code", "INDEX_PARSE_ERROR")})
        # Deletion, disclosure changes, invalid syntax and newly sensitive content
        # all evict stale entries before any query is answered.
        self.cache = active
        self.definitions = [item for _, index, _ in active.values() for item in index.definitions]
        self.by_id = {item["id"]: item for item in self.definitions}
        self.by_symbol = {}
        self.modules = {}
        self.scopes_by_owner = {}
        for _, index, _ in active.values():
            self.modules.setdefault(index.module, []).append(index.root)
            path_module = index.path.rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")
            if path_module != index.module:
                self.modules.setdefault(path_module, []).append(index.root)
            for scope in index.scopes:
                if scope.kind in {"module", "function", "class"}:
                    self.scopes_by_owner[scope.owner] = scope
        for item in self.definitions:
            self.by_symbol.setdefault(item["symbol"], []).append(item["id"])
            path_module = item["path"].rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")
            alias = path_module + "." + item["qualname"]
            if alias != item["symbol"]:
                self.by_symbol.setdefault(alias, []).append(item["id"])
        self.references = []
        self.imports = []
        for _, index, _ in active.values():
            self.imports.extend(index.imports)
            for ref in index.references:
                self.check_budget()
                ids, reason = self.resolve_reference(ref)
                if ref["scope"].kind in {"lambda", "generator"}:
                    reason = "deferred_expression_body"
                targets = [self.by_id[identity] for identity in ids]
                uncertain = reason != "static_binding" or len(ids) != 1 or any(item["decorated"] or item["conditional"] for item in targets)
                clean = {key: value for key, value in ref.items() if key not in {"scope", "canonical", "runtime_import"}}
                clean.update(targets=ids, uncertain=uncertain, reason="decorated_binding" if any(item["decorated"] for item in targets) else "conditional_binding" if any(item["conditional"] for item in targets) else reason)
                self.references.append(clean)
        self.coverage = {"indexed_files": len(active), "bytes": consumed, "nodes": nodes,
                         "skipped": skipped, "errors": errors, "error_count": len(errors),
                         "limited": limited, "complete": not limited and not errors,
                         "notice": "Static bindings/candidates only; reflection, callbacks, dynamic dispatch and runtime imports require runtime evidence. Columns are UTF-8 byte offsets."}

    def lookup(self, scope, name):
        start = scope
        while scope:
            if name in scope.globals:
                while scope.parent:
                    scope = scope.parent
            elif name in scope.nonlocals:
                scope = scope.parent
                while scope and scope.kind != "function":
                    scope = scope.parent
                continue
            if scope.wildcard:
                return "unknown", "wildcard_import"
            if name in scope.bindings:
                values = scope.bindings[name]
                return values[0] if len(values) == 1 else ("unknown", "ambiguous_or_rebound_binding")
            scope = scope.parent
            if start.kind in {"function", "lambda", "comprehension", "generator"}:
                while scope and scope.kind == "class":
                    scope = scope.parent
        return "unknown", "external_or_unresolved"

    def canonical(self, name, seen=None):
        seen = set() if seen is None else seen
        if name in seen or len(seen) >= 20:
            return [], "import_cycle_or_depth_limit"
        seen.add(name)
        if name in self.by_symbol:
            ids = self.by_symbol[name]
            for identity in ids:
                definition = self.by_id[identity]
                parent = self.scopes_by_owner[definition["parent"]]
                bindings = parent.bindings.get(definition["name"], [])
                if len(bindings) != 1 or bindings[0] != ("definition", identity) or parent.wildcard:
                    return [], "ambiguous_or_rebound_binding"
            return ids, "static_binding" if len(ids) == 1 else "ambiguous_definition"
        # Follow package/module re-exports and module aliases without importing.
        parts = name.split(".")
        for split in range(len(parts) - 1, 0, -1):
            prefix, local = ".".join(parts[:split]), parts[split]
            scopes = self.modules.get(prefix, [])
            if len(scopes) == 1:
                kind, target = self.lookup(scopes[0], local)
                if kind in {"import", "runtime_import"}:
                    suffix = ".".join(parts[split + 1:])
                    ids, reason = self.canonical(target + ("." + suffix if suffix else ""), seen)
                    return ids, "runtime_import" if kind == "runtime_import" else reason
                if kind == "unknown" and target != "external_or_unresolved":
                    return [], target
        return [], "external_or_unresolved"

    def resolve_reference(self, ref):
        if "canonical" in ref:
            ids, reason = self.canonical(ref["canonical"])
            return ids, "runtime_import" if ref["runtime_import"] else reason
        parts = ref["expression"].split(".")
        scope = ref["scope"]
        kind, value = self.lookup(scope, parts[0])
        if kind == "definition":
            target = self.by_id[value]
            ids, reason = ([value], "static_binding") if len(parts) == 1 else self.canonical(target["symbol"] + "." + ".".join(parts[1:]))
            if target["decorated"]:
                reason = "decorated_binding"
            return ids, reason
        if kind in {"import", "runtime_import"}:
            ids, reason = self.canonical(value + ("." + ".".join(parts[1:]) if len(parts) > 1 else ""))
            return ids, "runtime_import" if kind == "runtime_import" else reason
        if parts[0] in {"self", "cls"} and len(parts) == 2 and value == "parameter":
            parent = scope.parent
            if parent and parent.kind == "class":
                owner = self.by_id.get(parent.owner)
                if owner:
                    ids, _ = self.canonical(owner["symbol"] + "." + parts[1])
                    return ids, "dynamic_receiver"
        return [], "dynamic_expression" if ref["expression"] == "<dynamic-expression>" else value

    @result_boundary
    def query(self, operation, symbol, path=None):
        if operation not in {"find_definition", "find_references", "find_callers", "find_callees", "find_related_tests"}:
            raise ToolError("UNKNOWN_SYMBOL_QUERY", "Unsupported symbol query")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 256 or any(c in symbol for c in "\n\r\t"):
            raise ToolError("INVALID_SYMBOL", "Symbol must be 1–256 characters on one line")
        if path is not None:
            path = self.files.policy.resolve(path).relative_to(self.files.policy.root).as_posix()
            if isinstance(self.files, ScopedFiles) and path not in self.files.allowed:
                raise ToolError("SEND_SCOPE_DENIED", "Symbol path is outside the approved scope")
        self.refresh()
        definitions = [item for item in self.definitions if symbol in {item["id"], item["name"], item["qualname"], item["symbol"]}
                       and (path is None or item["path"] == path)]
        if not definitions:
            aliases, _ = self.canonical(symbol)
            definitions = [self.by_id[identity] for identity in aliases
                           if path is None or self.by_id[identity]["path"] == path]
        identities = {item["id"] for item in definitions}
        if operation == "find_definition":
            matches = definitions
        elif operation == "find_callees":
            matches = [ref for ref in self.references if ref["kind"] == "call" and ref["owner"] in identities]
        elif operation == "find_related_tests":
            matches = self.related_tests(identities)
        else:
            matches = [ref for ref in self.references if identities.intersection(ref["targets"])
                       and (operation == "find_references" or ref["kind"] == "call")]
        matches = sorted(matches, key=lambda item: (item["path"], item["line"], item.get("column", 0)))
        selected = []
        data = {"query": operation, "symbol": symbol, "definitions": definitions,
                "matches": selected, "ambiguous": len(definitions) > 1, "coverage": self.coverage}
        truncated = self.coverage["limited"]
        limit = self.files.config.max_tool_output_chars
        # Diagnostics and candidate definitions are also part of the output budget.
        while len(json.dumps(data, ensure_ascii=False)) > limit and data["coverage"]["errors"]:
            data["coverage"]["errors"].pop()
            truncated = True
        while len(json.dumps(data, ensure_ascii=False)) > limit and data["definitions"]:
            data["definitions"].pop()
            truncated = True
        if len(json.dumps(data, ensure_ascii=False)) > limit:
            raise ToolError("SYMBOL_OUTPUT_TOO_LARGE", "Symbol query metadata exceeds output budget")
        for match in matches:
            if len(selected) >= self.files.config.max_search_hits:
                truncated = True
                break
            selected.append(match)
            if len(json.dumps(data, ensure_ascii=False)) > limit:
                selected.pop()
                truncated = True
                break
        for item in data["definitions"] + selected:
            self.files.read_paths.add(item["path"])
        return ToolResult(True, data, truncated=truncated)

    def related_tests(self, identities):
        # Reverse reference closure includes imports, fixtures and callbacks as
        # candidate relationships; it does not claim test coverage or execution.
        reached = {identity: (0, False) for identity in identities}
        refs = self.references
        for depth in range(1, 9):
            self.check_budget()
            added = {}
            for ref in refs:
                targets = set(ref["targets"]) & reached.keys()
                if targets and ref["owner"] not in reached:
                    uncertain = ref["uncertain"] or all(reached[target][1] for target in targets)
                    added[ref["owner"]] = (depth, uncertain)
            if not added:
                break
            reached.update(added)
        self.coverage["related_tests_max_depth"] = 8
        self.coverage["related_tests_depth_limited"] = any(
            set(ref["targets"]) & reached.keys() and ref["owner"] not in reached for ref in refs)
        if self.coverage["related_tests_depth_limited"]:
            self.coverage["limited"] = True
            self.coverage["complete"] = False
        matches = []
        for definition in self.definitions:
            if definition["id"] in reached and definition["is_test"] and definition["name"].startswith("test"):
                depth, uncertain = reached[definition["id"]]
                matches.append({**definition, "distance": depth, "uncertain": uncertain,
                                "reason": "static_reference_candidate_not_test_coverage"})
        return matches
