"""Installed, independent JSONL service: versioned approvals and restart recovery."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import mini_agent


def main():
    repo = Path(__file__).resolve().parents[1]
    assert Path(mini_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), "Run with an installed package and PYTHONPATH cleared"
    case = uuid.uuid4().hex[:12]
    project = repo / ".pytest-tmp/installed-core6" / case / "project"
    runs = repo / ".mini-agent/installed-core6" / case
    shutil.copytree(repo / "examples/buggy_project", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mini-agent"))
    source = project / "src/calculator.py"
    original = source.read_bytes()
    run_id = None
    for operation in ("start", "recover"):
        process = subprocess.Popen([sys.executable, "-u", "-m", "mini_agent.service"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env={**os.environ, "PYTHONPATH": ""})
        def send(**data):
            process.stdin.write(json.dumps({"version": 1, **data}) + "\n")
            process.stdin.flush()
        try:
            argv = ["--project", str(project), "--task", "average([]) returns 0", "--runs-dir", str(runs)] if operation == "start" else ["--runs-dir", str(runs), "--run-id", run_id]
            send(type=operation, argv=argv, request_id=operation)
            stale_checked = False
            requested = None
            while True:
                line = process.stdout.readline()
                assert line, "Service exited without a result"
                event = json.loads(line)
                if event["type"] == "run_ready":
                    run_id = event["run_id"]
                if event["type"] == "approval_requested":
                    requested = event
                    send(type="approval", task_id=event["task_id"], approval_id=event["approval_id"],
                         proposal_version="stale", approved=True)
                if event["type"] == "command_error" and requested:
                    stale_checked = True
                    assert (source.read_bytes() == original) if operation == "start" else (source.read_bytes() != original)
                    send(type="approval", task_id=requested["task_id"], approval_id=requested["approval_id"],
                         proposal_version=requested["proposal_version"], approved=True)
                if event["type"] == "process_result":
                    assert stale_checked
                    assert event["exit_code"] == (2 if operation == "start" else 0)
                    break
            if operation == "start":
                assert source.read_bytes() != original
                send(type="events", runs_dir=str(runs), run_id=run_id, request_id="events", limit=1000)
                while True:
                    response = json.loads(process.stdout.readline())
                    if response.get("request_id") == "events":
                        events = response["result"]["events"]
                        assert any(e["event"] == "approval_requested" and e["proposal_version"] == requested["proposal_version"] for e in events)
                        assert events[-1]["event"] == "task_stopped"
                        break
            else:
                assert source.read_bytes() == original
        finally:
            process.stdin.close()
            try:
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()
    print(json.dumps({"version": mini_agent.__version__, "installed": mini_agent.__file__,
        "verification": "independent service, stale approval refusal, event replay, restart recovery", "artifact": str(runs / run_id)}))


if __name__ == "__main__":
    main()
