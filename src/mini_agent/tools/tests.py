"""Authorized fixed pytest execution with bounded output and process-tree cleanup."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from ..config import AgentConfig
from ..models import ToolError
from ..policy import PolicyGuard
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


def build_test_environment(report: Path) -> dict[str, str]:
    # Allowlist instead of inheriting model/cloud credentials, proxies or pytest arguments.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
               "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TZ"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
                "PYTEST_PLUGINS": "mini_agent.pytest_reporter",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
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

    def run(self, report: Path, *, cancelled=lambda: False, timeout: float | None = None) -> TestResult:
        if not self.authorized:
            raise ToolError("TESTS_NOT_AUTHORIZED", "Explicit test authorization is required")
        if report.exists() or report.is_symlink():
            raise ToolError("TEST_REPORT_EXISTS", "Each test invocation requires a fresh report path")
        interpreter = validate_interpreter(self.interpreter)
        self.policy.resolve("tests", directory=True)
        self.policy.resolve(".", directory=True)
        command = [str(interpreter), "-m", "pytest", "tests", "-q"]
        result = TestResult(command)
        if cancelled():
            result.cancelled = True
            return result
        budget = min(self.config.test_timeout_seconds, timeout if timeout is not None else self.config.test_timeout_seconds)
        if budget <= 0:
            result.timed_out = True
            return result
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
        except (OSError, ValueError, AttributeError, TypeError):
            result.error = "INVALID_TEST_REPORT"
        return result


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
