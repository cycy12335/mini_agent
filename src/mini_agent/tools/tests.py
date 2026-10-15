"""Authorized fixed pytest execution with bounded output and process-tree cleanup."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import re
import tomllib
import configparser

from ..config import AgentConfig
from ..models import ToolError
from ..policy import PolicyGuard, _check_links
from ..patching import atomic_write
from .files import FileTools
from .processes import PosixGroup, WindowsJob


def validate_interpreter(path: Path) -> Path:
    # Preserve the POSIX venv executable symlink: resolving it selects the base Python.
    interpreter = path.absolute()
    environment = interpreter.parent.parent
    if (interpreter.parent.name.lower() not in {"scripts", "bin"}
            or not interpreter.name.lower().startswith("python")
            or interpreter.suffix.lower() in {".bat", ".cmd", ".ps1"}
            or not interpreter.is_file() or not (environment / "pyvenv.cfg").is_file()):
        raise ToolError("INVALID_INTERPRETER", "Select an existing Python executable in a virtual environment")
    return interpreter


def select_pytest_configuration(policy, config):
    files = FileTools(policy, config)
    for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
        try:
            path = policy.resolve(name)
            _, text = files._read(name)
        except FileNotFoundError:
            continue
        try:
            if name == "pytest.ini":
                ini = configparser.ConfigParser(interpolation=None)
                ini.read_string(text)
                return path
            if name == "pyproject.toml":
                if "pytest" in tomllib.loads(text).get("tool", {}):
                    return path
            else:
                ini = configparser.ConfigParser(interpolation=None)
                ini.read_string(text)
                if ini.has_section("tool:pytest" if name == "setup.cfg" else "pytest"):
                    return path
        except (ValueError, configparser.Error, AttributeError, TypeError) as exc:
            raise ToolError("INVALID_TEST_CONFIGURATION", "Root pytest configuration cannot be parsed safely") from exc
    return None


def build_test_environment(report: Path) -> dict[str, str]:
    # Allowlist instead of inheriting model/cloud credentials, proxies or pytest arguments.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
               "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TZ"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
                "PYTEST_PLUGINS": "mini_agent_pytest_reporter",
                "PYTHONPATH": str(report.parent),
                "MINI_AGENT_TEST_REPORT": str(report)})
    return env


@dataclass
class TestResult:
    __test__ = False
    command: list[str]
    exit_code: int | None = None
    output: str = ""
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    cleanup_ok: bool = True
    error: str | None = None
    complete: bool = False
    cases: dict[str, str] | None = None
    collection_errors: list[str] | None = None
    failure_types: dict[str, str] | None = None
    scope: dict | None = None

    @property
    def passed(self):
        return (self.exit_code == 0 and not self.error and not self.timed_out and not self.cancelled
                and self.cleanup_ok and self.complete and bool(self.cases)
                and "passed" in self.cases.values()
                and all(value in {"passed", "skipped"} for value in self.cases.values())
                and not self.collection_errors)

    def to_dict(self):
        return {**asdict(self), "passed": self.passed}


class TestRunner:
    __test__ = False

    def __init__(self, policy: PolicyGuard, interpreter: Path, config: AgentConfig, *, authorized=False):
        self.policy, self.interpreter, self.config = policy, interpreter, config
        self.authorized = authorized

    def run(self, report: Path, *, cancelled=lambda: False, timeout: float | None = None,
            targets: list[str] | None = None) -> TestResult:
        if not self.authorized:
            raise ToolError("TESTS_NOT_AUTHORIZED", "Explicit test authorization is required")
        if report.exists() or report.is_symlink():
            raise ToolError("TEST_REPORT_EXISTS", "Each test invocation requires a fresh report path")
        interpreter = validate_interpreter(self.interpreter)
        selected = self.validate_targets(targets or ["tests"])
        self.policy.resolve(".", directory=True)
        configuration = select_pytest_configuration(self.policy, self.config)
        if configuration is None:
            configuration = report.parent / "mini_agent_pytest_empty.ini"
            _check_links(report.parent)
            if configuration.exists():
                _check_links(configuration)
                if configuration.stat().st_nlink > 1:
                    raise ToolError("TEST_CONFIG_LINK", "Linked fallback test configuration is forbidden")
            atomic_write(configuration, b"[pytest]\n")
        command = [str(interpreter), "-m", "pytest", "-c", str(configuration), "--rootdir=.", *selected, "-q"]
        result = TestResult(command)
        if cancelled():
            result.cancelled = True
            return result
        budget = min(self.config.test_timeout_seconds, timeout if timeout is not None else self.config.test_timeout_seconds)
        if budget <= 0:
            result.timed_out = True
            return result
        # Inject only the stdlib-only reporter, never Agent's site-packages/dependencies.
        _check_links(report.parent)
        plugin = report.parent / "mini_agent_pytest_reporter.py"
        if plugin.exists() or plugin.is_symlink():
            _check_links(plugin)
            if plugin.stat().st_nlink > 1:
                raise ToolError("TEST_PLUGIN_LINK", "Linked test reporters are forbidden")
        atomic_write(plugin, (Path(__file__).resolve().parents[1] / "pytest_reporter.py").read_bytes())
        process = tree = reader = None
        captured = bytearray()
        total = 0
        read_error = []
        def drain():
            nonlocal total
            try:
                while chunk := process.stdout.read1(4096):
                    total += len(chunk)
                    remaining = self.config.max_test_output_bytes - len(captured)
                    if remaining > 0:
                        captured.extend(chunk[:remaining])
            except OSError:
                read_error.append(True)
        try:
            tree = WindowsJob() if os.name == "nt" else PosixGroup()
            options = {"creationflags": 0x4 | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(command, cwd=self.policy.root, env=build_test_environment(report),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       shell=False, **options)
            tree.attach_and_resume(process.pid)
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + budget
            while process.poll() is None:
                if cancelled():
                    result.cancelled = True
                    break
                if time.monotonic() >= deadline:
                    result.timed_out = True
                    break
                time.sleep(0.02)
        except KeyboardInterrupt:
            result.cancelled = True
        except (OSError, ValueError) as exc:
            result.error = f"TEST_PROCESS_ERROR ({type(exc).__name__})"
        finally:
            try:
                if tree is not None:
                    tree.stop()  # Also clean descendants when pytest itself exits normally.
            except OSError:
                result.cleanup_ok = False
            finally:
                if tree is not None:
                    tree.close()
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    result.exit_code = process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    result.cleanup_ok = False
                if reader is not None:
                    reader.join(timeout=5)
                if reader is None or not reader.is_alive():
                    process.stdout.close()
                else:
                    result.cleanup_ok = False
        result.truncated = total > self.config.max_test_output_bytes
        result.output = bytes(captured).decode("utf-8", errors="ignore")
        if read_error:
            result.error = "OUTPUT_READ_ERROR"
        try:
            if report.is_file() and report.stat().st_size <= 256 * 1024:
                data = json.loads(report.read_text(encoding="utf-8"))
                cases, errors = data.get("cases"), data.get("collection_errors")
                if (data.get("version") == 1 and data.get("exit_code") == result.exit_code
                        and isinstance(cases, dict) and isinstance(errors, list)
                        and all(isinstance(k, str) and v in {"passed", "failed", "error", "skipped"} for k, v in cases.items())
                        and all(isinstance(v, str) for v in errors)):
                    result.complete = data.get("complete") is True
                    result.cases, result.collection_errors = cases, errors
                    failures = data.get("failure_types", {})
                    if isinstance(failures, dict) and all(k in cases and isinstance(v, str) and len(v) <= 128 for k, v in failures.items()):
                        result.failure_types = failures
        except (OSError, ValueError, AttributeError, TypeError):
            result.error = "INVALID_TEST_REPORT"
        return result

    def validate_targets(self, targets):
        if not isinstance(targets, list) or not 1 <= len(targets) <= self.config.max_test_targets:
            raise ToolError("INVALID_TEST_TARGET", "Test selectors exceed the allowed count")
        selected = []
        for target in targets:
            if not isinstance(target, str) or not target or len(target) > 1536 or any(ord(c) < 32 for c in target):
                raise ToolError("INVALID_TEST_TARGET", "Invalid test selector")
            parts = target.split("::")
            path = parts[0]
            if path.startswith("-") or any(not re.fullmatch(r"[A-Za-z_]\w*", node) for node in parts[1:]):
                raise ToolError("INVALID_TEST_TARGET", "Only paths and literal Python test names are selectable")
            if len(parts) > 3:
                raise ToolError("INVALID_TEST_TARGET", "Test selector nesting is unsupported")
            try:
                self.policy.resolve(path, directory=True)
                if len(parts) != 1:
                    raise ToolError("INVALID_TEST_TARGET", "Node selectors require a Python file")
            except ToolError as exc:
                if exc.code != "NOT_DIRECTORY":
                    raise
                resolved = self.policy.resolve(path)
                if resolved.suffix != ".py":
                    raise ToolError("INVALID_TEST_TARGET", "Test files must be Python source")
            if target not in selected:
                selected.append(target)
        return selected


def compare_results(baseline: TestResult, latest: TestResult) -> dict:
    old = {k for k, v in (baseline.cases or {}).items() if v in {"failed", "error"}}
    new = {k for k, v in (latest.cases or {}).items() if v in {"failed", "error"}}
    fixed = {k for k in old if (latest.cases or {}).get(k) == "passed"}
    return {"comparable": baseline.complete and latest.complete,
            "original_failures": sorted(old), "fixed_failures": sorted(fixed),
            "remaining_failures": sorted(old & new), "new_failures": sorted(new - old),
            "unresolved_failures": sorted(old - fixed),
            "missing_cases": sorted(set(baseline.cases or {}) - set(latest.cases or {})),
            "newly_skipped": sorted(k for k, v in (baseline.cases or {}).items()
                                    if v != "skipped" and (latest.cases or {}).get(k) == "skipped"),
            "baseline_collection_errors": baseline.collection_errors or [],
            "latest_collection_errors": latest.collection_errors or []}
