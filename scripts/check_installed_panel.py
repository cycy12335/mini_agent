"""Verify the wheel-installed stdio entry point without network or model credentials."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import uuid

import mini_agent


def main():
    repo = Path(__file__).resolve().parents[1]
    installed = Path(mini_agent.__file__).resolve()
    if not installed.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Install the wheel without -e before this check")
    key = uuid.uuid4().hex[:12]
    work = repo / "examples" / f"demo-wheel-panel-{key}"
    runs = repo / ".mini-agent" / "wheel-panel-check" / key
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment["PYTHONIOENCODING"] = "utf-8"
    for mode, expected, code in (("readonly", "proposed", 0), ("approve", "unverified", 2), ("cancel", "cancelled", 3)):
        project = work / mode
        shutil.copytree(repo / "examples/buggy_project", project,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        source = project / "src/calculator.py"
        original = source.read_bytes()
        args = [sys.executable, "-u", "-m", "mini_agent.panel", "--project", str(project),
                "--task", "修复 average([])", "--model", "mock", "--runs-dir", str(runs / mode)]
        if mode == "readonly":
            args.append("--read-only")
        process = subprocess.Popen(args, cwd=project, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        events = queue.Queue()
        def reader():
            for line in process.stdout:
                events.put(line)
            events.put(None)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            sequence, preview, complete = 0, False, False
            while True:
                line = events.get(timeout=20)
                if line is None:
                    break
                event = json.loads(line)
                assert event["version"] == 1 and event["seq"] > sequence
                sequence = event["seq"]
                if event["type"] == "proposal":
                    preview = True
                if event["type"] == "approval_requested":
                    assert source.read_bytes() == original
                    command = {"version": 1, "type": "cancel"} if mode == "cancel" else {
                        "version": 1, "type": "approval", "approval_id": event["approval_id"], "approved": True}
                    process.stdin.write(json.dumps(command) + "\n")
                    process.stdin.flush()
                if event["type"] == "process_result":
                    assert event["exit_code"] == code
                    complete = True
            assert process.wait(timeout=5) == code, process.stderr.read()
            assert preview and complete
            summary = json.loads(next((runs / mode).glob("*/summary.json")).read_text(encoding="utf-8"))
            assert summary["status"] == expected
            assert (source.read_bytes() != original) is (mode == "approve")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            thread.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    # Restore the approved run through the installed module, retaining its historical report.
    repaired = work / "approve"
    directory = next((runs / "approve").iterdir())
    historical_report = (directory / "report.md").read_bytes()
    process = subprocess.Popen([sys.executable, "-u", "-m", "mini_agent.panel", "rollback",
        "--runs-dir", str(runs / "approve"), "--run-id", directory.name], cwd=repaired,
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    watchdog = threading.Timer(20, process.kill)
    watchdog.start()
    try:
        restored = False
        for line in process.stdout:
            event = json.loads(line)
            if event["type"] == "approval_requested":
                assert event["kind"] == "rollback"
                process.stdin.write(json.dumps({"version": 1, "type": "approval",
                    "approval_id": event["approval_id"], "approved": True}) + "\n")
                process.stdin.flush()
            if event["type"] == "recovery_result":
                assert event["status"] == "rolled_back"
                restored = True
        assert process.wait(timeout=5) == 0, process.stderr.read()
        assert restored
        assert (repaired / "src/calculator.py").read_bytes() == (repo / "examples/buggy_project/src/calculator.py").read_bytes()
        assert (directory / "report.md").read_bytes() == historical_report
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    print(f"Wheel {mini_agent.__version__} verified: readonly preview, panel approval, cancellation, approved recovery.")
    print(f"Installed package: {installed}")
    print(f"Evidence: {runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
