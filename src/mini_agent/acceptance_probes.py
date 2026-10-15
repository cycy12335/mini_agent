"""Deterministic reliability probes, deliberately excluded from live repair metrics."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .acceptance import save, suite, oracle_test
from .adapters.http_api import HTTPAdapter
from .adapters.mock import call
from .api_config import APIConfig
from .audit import AuditLog
from .config import AgentConfig
from .controller import AgentController
from .history import task_status
from .models import ToolError
from .patching import PatchManager
from .policy import PolicyGuard
from .tools.files import FileTools
from .tools.registry import ToolRegistry
from .tools.tests import TestRunner


class ProbeAdapter:
    """Discover a source and propose a harmless comment, never a canned bug fix."""
    def __init__(self, failure=False):
        self.step, self.failure = 0, failure

    def next_action(self, messages, schemas):
        self.step += 1
        if self.failure:
            raise ToolError("MODEL_NETWORK_ERROR", "Injected offline model failure")
        if self.step == 1:
            return call("list_files")
        result = next(m["result"]["data"] for m in reversed(messages) if m.get("role") == "tool")
        if self.step == 2:
            return call("read_file", path=next(name for name in result["files"] if name.startswith("src/") and name.endswith(".py")))
        if self.step == 3:
            return call("propose_edits", edits=[{"path": result["path"], "original_sha256": result["sha256"],
                "content": result["content"] + "\n# offline ownership probe\n"}])
        return {"type": "final", "message": "Offline reliability probe, no repair claim."}


def copy_project(source, target):
    files = FileTools(PolicyGuard(source), AgentConfig())
    listing = files.list_files()
    if not listing.ok or listing.truncated:
        raise ValueError("Probe copy incomplete")
    target.mkdir(parents=True)
    for name in listing.data["files"]:
        raw, _ = files._read(name)
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def interrupted_probe(root, case):
    project = root / "probes/interrupted/project"
    copy_project(root / "projects" / case["id"], project)
    runs = root / "probe-runs/interrupted"
    script = '''
from pathlib import Path
import sys, time
from mini_agent.audit import AuditLog
from mini_agent.history import TaskLifecycle
from mini_agent.locking import RepositoryLease
from mini_agent.patching import PatchManager, digest
from mini_agent.policy import PolicyGuard
project, runs = map(Path, sys.argv[1:])
policy = PolicyGuard(project)
with RepositoryLease(project):
    audit = AuditLog(runs, policy.root)
    with TaskLifecycle(audit, policy.root):
        patches = PatchManager(policy, audit.directory, read_only=False)
        source = project / 'src/ledger/maths.py'
        raw = source.read_bytes()
        proposal = patches.preview([{'path': 'src/ledger/maths.py', 'original_sha256': digest(source.read_bytes()), 'content': raw.decode() + '\\n# interrupted probe\\n'}])
        patches.apply(proposal, True)
        print(audit.run_id, flush=True)
        time.sleep(120)
'''
    original = (project / "src/ledger/maths.py").read_bytes()
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(project), str(runs)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(child.stdout.readline().strip()), daemon=True)
    reader.start()
    try:
        run_id = ready.get(timeout=15)
        if not run_id:
            raise ValueError("Interrupted probe child failed before readiness")
        audit = AuditLog.open(runs, run_id)
        active = task_status(audit)["status"] == "running"
        child.kill()
        child.wait(timeout=5)
        interrupted = task_status(audit)
        patches = PatchManager(PolicyGuard(project), audit.directory, read_only=False)
        patches.rollback_preview()
        patches.rollback(True)
        return {"case_id": "interrupted", "backend": "offline_invariant", "artifact": str(audit.directory),
            "passed": active and interrupted["status"] == "interrupted" and interrupted["recovery_available"]
                and (project / "src/ledger/maths.py").read_bytes() == original,
            "expected": "owner death detected and applied files recovered", "observed": interrupted["status"]}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
        reader.join(timeout=2)


def run_offline(root):
    root = Path(root).absolute()
    manifest = suite(root)
    if (root / "offline-results.json").exists() or (root / "probes").exists():
        raise ValueError("Offline attempts are immutable; prepare a fresh suite")
    cases = {case["id"]: case for case in manifest["cases"]}
    records = []
    baselines = []
    # Validate all three hidden oracles actually reject the original defects.
    for case in manifest["cases"]:
        if case.get("oracle_sha256"):
            result = oracle_test(root, case, "before")
            baselines.append({"case_id": case["id"], "passed": result["exit_code"] == 1 and result["complete"]
                and not result["collection_errors"], "artifact": result["artifact"]})
    for scenario in ("missing_dependency", "user_modification", "cancelled", "model_failure", "cost_limit", "action_limit", "time_limit"):
        case = cases["unavailable_dependency"] if scenario == "missing_dependency" else cases["batch_total"]
        project = root / "probes" / scenario / "project"
        copy_project(root / "projects" / case["id"], project)
        policy, config, adapter = PolicyGuard(project), AgentConfig(), ProbeAdapter(failure=scenario == "model_failure")
        audit = AuditLog(root / "probe-runs" / scenario, policy.root)
        cancelled = threading.Event()
        user_changes = {}
        initial = {name: path.read_bytes() for name in case["initial_hashes"] for path in [project / name]}
        def approve(proposal):
            if scenario == "user_modification":
                edit = proposal.edits[0]
                changed = (project / edit.path).read_bytes() + b"\n# user changes must survive\n"
                (project / edit.path).write_bytes(changed)
                user_changes[edit.path] = changed
            if scenario == "cancelled":
                cancelled.set()
            return True
        if scenario == "action_limit":
            config = replace(config, max_actions=1)
        if scenario == "cost_limit":
            class NoNetwork:
                def post(self, *args):
                    raise AssertionError("Cost limit must stop before network")
            adapter = HTTPAdapter(APIConfig(endpoint="http://127.0.0.1/unused", model="offline-probe",
                protocol="chat_completions", max_run_cost_cny=0.000001,
                input_price_cny_per_million=2, output_price_cny_per_million=8), key="offline-only-placeholder",
                authorized=True, transport=NoNetwork())
        options = {}
        if scenario == "time_limit":
            config = replace(config, run_timeout_seconds=1)
            ticks = iter(range(1000))
            options["clock"] = lambda: next(ticks) * 2
        runner = None if scenario == "time_limit" else TestRunner(policy, Path(case["interpreter"]), config, authorized=True)
        agent = AgentController(adapter, ToolRegistry(FileTools(policy, config)), audit, config,
            read_only=False, approve=approve, cancelled=cancelled.is_set, test_runner=runner, **options)
        started = time.monotonic()
        result = agent.run(case["task"])
        expected = {"missing_dependency": "test_dependency_unavailable", "user_modification": "hash_conflict",
            "cancelled": "cancelled", "model_failure": "model_network_error", "cost_limit": "model_cost_limit",
            "action_limit": "action_limit", "time_limit": "time_limit"}[scenario]
        same = all((project / name).read_bytes() == user_changes.get(name, raw) for name, raw in initial.items())
        observed = result.status if scenario == "cancelled" else result.reason
        passed = observed == expected and same
        if scenario == "missing_dependency":
            passed = passed and adapter.step == 0
        if scenario == "cost_limit":
            passed = passed and adapter.requests == 0
        records.append({"case_id": scenario, "backend": "offline_invariant", "passed": passed,
            "expected": expected, "observed": observed, "source_preserved": same,
            "elapsed_seconds": round(time.monotonic() - started, 3), "artifact": str(audit.directory)})
        save(root / "offline-results.json", {"suite_id": manifest["suite_id"], "oracle_baselines": baselines, "records": records})
    records.append(interrupted_probe(root, cases["batch_total"]))
    result = {"suite_id": manifest["suite_id"], "backend": "offline_invariant", "oracle_baselines": baselines,
        "records": records, "passed": all(row["passed"] for row in records + baselines),
        "live_model_evaluated": False, "repair_success_rate": None, "diagnosis_accuracy": None}
    save(root / "offline-results.json", result)
    return result
