"""Development/CI smoke check of a wheel-installed CLI. Never calls a model API."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import venv

import mini_agent


def main():
    repository = Path(__file__).resolve().parents[1]
    if not Path(mini_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError('Install a wheel first with python -m pip install ".[dev]" (without -e)')
    identifier = uuid.uuid4().hex[:12]
    work = repository / "examples" / f"demo-stage6-cli-{identifier}"
    runs = repository / ".mini-agent" / "installed-cli" / identifier
    shutil.copytree(repository / "examples/buggy_project", work / "average",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    shutil.copytree(repository / "examples/missing_dependency_project", work / "missing",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    python = Path(sys.executable)
    if sys.prefix == sys.base_prefix:
        test_environment = work / ".venv"
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(test_environment)
        python = test_environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def command(*args, answer=""):
        result = subprocess.run([sys.executable, "-m", "mini_agent", *args], input=answer,
                                capture_output=True, encoding="utf-8", timeout=60,
                                cwd=work, env={**os.environ, "PYTHONUTF8": "1"})
        return result

    def summary(directory):
        path = next(directory.glob("*/summary.json"))
        return path.parent.name, json.loads(path.read_text(encoding="utf-8"))

    source = work / "average/src/calculator.py"
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    common = ("run", "--project", str(work / "average"), "--task", "average([]) returns zero", "--model", "mock")
    readonly = command(*common, "--read-only", "--runs-dir", str(runs / "readonly"))
    _, readonly_state = summary(runs / "readonly")
    assert readonly.returncode == 0 and readonly_state["status"] == "proposed"
    assert readonly_state["tests"] == "not_run" and hashlib.sha256(source.read_bytes()).hexdigest() == original
    repaired = command(*common, "--allow-tests", "--test-python", str(python),
                       "--runs-dir", str(runs / "repair"), answer="y\n")
    run_id, repaired_state = summary(runs / "repair")
    assert repaired.returncode == 0 and repaired_state["status"] == "verified", repaired.stdout + repaired.stderr
    assert [r["exit_code"] for r in repaired_state["test_history"]] == [1, 0]
    restored = command("rollback", "--run-id", run_id, "--runs-dir", str(runs / "repair"), answer="y\n")
    assert restored.returncode == 0 and hashlib.sha256(source.read_bytes()).hexdigest() == original
    missing = command("run", "--project", str(work / "missing"), "--task", "Investigate missing dependency",
                      "--model", "mock", "--allow-tests", "--test-python", str(python), "--runs-dir", str(runs / "missing"))
    _, missing_state = summary(runs / "missing")
    assert missing.returncode == 1 and missing_state["reason"] == "test_dependency_unavailable", missing.stdout + missing.stderr
    assert missing_state["actions"] == 0 and missing_state["modified_files"] == []
    print("Installed CLI verified: readonly, approved repair, tests, rollback, missing-dependency stop.")
    print(f"Evidence: {runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
