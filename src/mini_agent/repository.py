"""Bounded, read-only Python repository discovery; never import project code."""

import configparser
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib

from .models import ToolError
from .policy import _check_links
from .privacy import Redactor
from .tools.files import FileTools
from .tools.tests import validate_interpreter


def discover_repository(files: FileTools, *, local: bool = True) -> dict:
    """Use the supplied file scope, including disclosure restrictions, for metadata."""
    names, truncated, skipped = files._scan(".")
    python = [name for name in names if name.endswith((".py", ".pyi"))]
    tests = [name for name in python if name.split("/")[-1].startswith("test_")
             or name.endswith("_test.py")]
    source = [name for name in python if name not in tests
              and Path(name).name not in {"conftest.py", "setup.py"}]
    metadata = {}
    warnings = []
    redactor = getattr(files, "redactor", None) or Redactor()
    configs = [name for name in names if Path(name).name in
               {"pyproject.toml", "pytest.ini", "setup.cfg", "setup.py", "tox.ini"}
               or Path(name).name.startswith("requirements") and name.endswith(".txt")]
    for name in configs:
        # setup.py is a declaration location only. Never execute or interpret it.
        if name.endswith("setup.py"):
            metadata[name] = {"kind": "executable_declaration_not_loaded"}
            continue
        try:
            _, text = files._read(name)
            if redactor.text(text) != text:
                warnings.append({"path": name, "code": "SENSITIVE_CONTENT"})
                continue
            if name.endswith("pyproject.toml"):
                data = tomllib.loads(text)
                project = data.get("project", {})
                tool = data.get("tool", {})
                metadata[name] = {
                    "name": project.get("name"), "requires_python": project.get("requires-python"),
                    "dependencies": project.get("dependencies", []),
                    "optional_dependencies": project.get("optional-dependencies", {}),
                    "pytest": tool.get("pytest", {}).get("ini_options", tool.get("pytest", {})),
                    "packages": tool.get("setuptools", {}).get("packages", {}),
                    "package_dir": tool.get("setuptools", {}).get("package-dir", {}),
                    "build_requirements": data.get("build-system", {}).get("requires", []),
                    "poetry_dependencies": tool.get("poetry", {}).get("dependencies", {}),
                }
            elif name.endswith(".txt"):
                metadata[name] = {"requirements": [line.strip() for line in text.splitlines()
                                                   if line.strip() and not line.lstrip().startswith("#")]}
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(text)
                section = "tool:pytest" if name.endswith("setup.cfg") else "pytest"
                metadata[name] = {"pytest": dict(parser[section]) if parser.has_section(section) else {}}
                if name.endswith("setup.cfg"):
                    metadata[name]["install_requires"] = parser.get("options", "install_requires", fallback="").splitlines()
                    metadata[name]["extras_require"] = dict(parser["options.extras_require"]) if parser.has_section("options.extras_require") else {}
        except (ToolError, OSError, ValueError, configparser.Error, AttributeError, TypeError) as exc:
            warnings.append({"path": name, "code": getattr(exc, "code", "INVALID_METADATA")})
    configured_roots = {"src": ""}
    for name, entry in metadata.items():
        if name != "pyproject.toml":
            continue  # nested project declarations have their own relative roots
        package_dirs = entry.get("package_dir", {})
        if isinstance(package_dirs, dict):
            for package, directory in package_dirs.items():
                if isinstance(package, str) and isinstance(directory, str):
                    configured_roots[directory] = package
        packages = entry.get("packages", {})
        find = packages.get("find", {}) if isinstance(packages, dict) else {}
        where = find.get("where", []) if isinstance(find, dict) else []
        if isinstance(where, list):
            for directory in where:
                if isinstance(directory, str):
                    configured_roots.setdefault(directory, "")
    safe_roots = {}
    for directory, package in configured_roots.items():
        try:
            resolved = files.policy.resolve(directory, directory=True)
            safe_roots[resolved.relative_to(files.policy.root).as_posix()] = package
        except (ToolError, OSError):
            continue
    roots = set()
    modules = []
    for name in source:
        candidates = [directory for directory in safe_roots
                      if directory == "." or name.startswith(directory + "/")]
        root = max(candidates, key=len) if candidates else name.split("/")[0] if "/" in name else "."
        roots.add(root)
        module_path = name if root not in safe_roots or root == "." else name[len(root) + 1:]
        module = module_path.rsplit(".", 1)[0].replace("/", ".").removesuffix(".__init__")
        if module == "__init__":
            module = ""
        prefix = safe_roots.get(root, "")
        modules.append({"path": name, "module": ".".join(value for value in (prefix, module) if value)})
    overview = {"version": 1, "language": "python" if python else "unknown",
                "source_roots": sorted(roots), "modules": modules, "test_files": tests,
                "module_resolution": "inferred_from_paths_not_runtime_imports",
                "configuration": metadata, "candidate_files": names,
                "truncated": truncated, "skipped": skipped, "warnings": warnings}
    if not python:
        warnings.append({"code": "NO_PYTHON_FILES"})
    if not tests:
        warnings.append({"code": "NO_TEST_FILES"})
    if tests and not any(name.startswith("tests/") for name in tests):
        warnings.append({"code": "NONSTANDARD_TEST_LAYOUT", "detail": "V1 runner still targets tests/; automatic test planning is Core-5."})
    if local:
        environments = discover_environments(files.policy.root)
        overview["environments"] = environments
        overview["selected_interpreter"] = environments[0] if len(environments) == 1 else None
        if len(environments) != 1:
            warnings.append({"code": "NO_VIRTUAL_ENVIRONMENT" if not environments else "AMBIGUOUS_VIRTUAL_ENVIRONMENT"})
        overview["git"] = discover_changes(files, names)
    # Do not let an inventory or configuration consume the whole model context.
    limit = min(files.config.max_tool_output_chars, files.config.max_context_chars // 2)
    while len(json.dumps(overview, ensure_ascii=False)) > limit:
        largest = max(("candidate_files", "modules", "test_files"), key=lambda key: len(overview[key]))
        if overview[largest]:
            overview[largest].pop()
        elif overview["configuration"]:
            overview["configuration"].popitem()
        else:
            raise ToolError("OVERVIEW_TOO_LARGE", "Repository overview exceeds configured output budget")
        overview["truncated"] = True
    return overview


def discover_environments(root: Path) -> list[str]:
    """Probe conventional local environments without reading hidden source files."""
    found = []
    for name in (".venv", "venv", "env", ".env"):
        environment = root / name
        try:
            _check_links(environment)
            _check_links(environment / "pyvenv.cfg")
            for executable in ("Scripts/python.exe", "bin/python"):
                candidate = environment / executable
                if candidate.is_file():
                    # POSIX venv Python is normally a symlink; validate_interpreter
                    # intentionally preserves it. Directory links remain forbidden.
                    _check_links(candidate.parent)
                    found.append(str(validate_interpreter(candidate)))
                    break
        except (OSError, ToolError):
            continue
    return found


def discover_changes(files: FileTools, names: list[str]) -> dict:
    """Git status only, bounded on disk; disclose only eligible inventory paths."""
    try:
        _check_links(files.policy.root / ".git")
        with tempfile.TemporaryFile() as stream:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(files.policy.root),
                 "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z",
                 "--untracked-files=all", "--", "."],
                stdout=stream, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if result.returncode:
                return {"status": "unavailable", "changes": []}
            stream.seek(0)
            raw = stream.read(files.config.max_tool_output_chars + 1)
        if len(raw) > files.config.max_tool_output_chars:
            return {"status": "truncated", "changes": []}
        entries = raw.decode("utf-8", errors="replace").split("\0")
        changes = []
        allowed = set(names)
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            eligible = path in allowed
            if not eligible and "D" in status:
                try:
                    files.policy.resolve(path)
                except FileNotFoundError:
                    eligible = True  # lexical/privacy checks ran before existence checks
                except (ToolError, OSError):
                    pass
            if eligible:
                changes.append({"path": path, "status": status})
            if "R" in status or "C" in status:
                index += 1  # porcelain -z rename source follows destination
        return {"status": "available", "changes": changes,
                "notice": "Only eligible existing files are listed; unsaved editor buffers are unknown."}
    except (OSError, ToolError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "changes": []}
