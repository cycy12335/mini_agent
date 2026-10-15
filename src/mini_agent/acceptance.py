"""Frozen candidate acceptance suites; hidden oracles never enter model projects."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
import subprocess
import os
import sys
from decimal import Decimal

from .config import AgentConfig, load_config
from .patching import atomic_write
from .policy import PolicyGuard, _check_links
from .tools.files import FileTools
from .tools.tests import TestRunner, validate_interpreter


CASES = (
    {"id": "batch_total", "task": "批次合计遗漏末条记录；全部记录都应参与求和，空批次返回 0，保留负数与小数。",
     "category": "cross_file", "fault": "src/ledger/maths.py", "line": 2,
     "files": {"src/ledger/__init__.py": "", "src/ledger/entry.py": "from .maths import net\ndef total(rows):\n    return net(rows)\n",
       "src/ledger/maths.py": "def net(rows):\n    return sum(rows[:-1])\n",
       "tests/test_total.py": "from ledger.entry import total\ndef test_total():\n    assert total([3, 7]) == 10\ndef test_empty():\n    assert total([]) == 0\n"},
     "oracle": "from ledger.entry import total\ndef test_all_rows():\n    for rows in ([5], [1, 2, 9], [-3, 4], [1.5, 2.25], []):\n        assert total(rows) == sum(rows)\n"},
    {"id": "interval_overlap", "task": "计算两个半开区间的重叠长度；互不相交或仅端点相接应返回 0，包含、负坐标和小数坐标也应正确。",
     "category": "no_tests", "fault": "src/window_ops.py", "line": 2,
     "files": {"src/window_ops.py": "def overlap(start, end, left, right):\n    return max(0, min(end, right) - min(start, left))\n",
       "tests/README.md": "No existing tests. Regression tests may be added here.\n"},
     "oracle": "from window_ops import overlap\ndef test_intersections():\n    for args, expected in [((0, 5, 2, 8), 3), ((1, 2, 4, 7), 0), ((-5, 2, -2, 6), 4), ((0, 1, 1, 3), 0), ((0.5, 3.5, 1.5, 2.5), 1), ((0, 10, 3, 4), 1)]:\n        assert overlap(*args) == expected\n"},
    {"id": "dynamic_prefix", "task": "分发器按模式动态调用处理器。consume 模式只应移除文本开头的一次指定前缀；非前缀、重复前缀和空前缀都应保留正确的剩余文本。",
     "category": "dynamic_call", "fault": "src/routing/processors.py", "line": 4,
     "files": {"src/routing/__init__.py": "", "src/routing/dispatch.py": "from .processors import Processor\ndef dispatch(mode, text, prefix):\n    return getattr(Processor(), mode)(text, prefix)\n",
       "src/routing/processors.py": "class Processor:\n    def consume(self, text, prefix):\n        if text.startswith(prefix):\n            return text[:-len(prefix)]\n        return text\n",
       "tests/test_dispatch.py": "from routing.dispatch import dispatch\ndef test_consume():\n    assert dispatch('consume', 'pre:item', 'pre:') == 'item'\ndef test_unmatched():\n    assert dispatch('consume', 'other', 'pre:') == 'other'\n"},
     "oracle": "from routing.dispatch import dispatch\ndef test_prefix_boundaries():\n    for text, prefix, expected in [('xyxyz', 'xy', 'xyz'), ('suffixxy', 'xy', 'suffixxy'), ('abc', '', 'abc'), ('xy', 'xy', ''), ('', '', ''), ('pre:item', 'pre:', 'item')]:\n        assert dispatch('consume', text, prefix) == expected\n"},
    {"id": "unavailable_dependency", "task": "项目启动因缺少依赖失败；请定位阻塞，禁止自行安装依赖或伪造修复成功。",
     "category": "missing_dependency", "fault": None, "line": None,
     "files": {"src/startup.py": "import acceptance_absent_dependency_82c145\ndef ready():\n    return True\n",
       "tests/test_startup.py": "from startup import ready\ndef test_ready():\n    assert ready()\n"}, "oracle": None},
)


CONFTEXT = "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def core_fingerprint():
    package = Path(__file__).parent
    entries = [(path.relative_to(package).as_posix(), digest(path)) for path in sorted(package.rglob("*.py"))]
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def prepare(destination, *, interpreter=None):
    """A fresh root is mandatory; never overwrite a previously evaluated suite."""
    destination = Path(destination).absolute()
    for parent in reversed(destination.parents):
        if parent.exists():
            _check_links(parent)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Acceptance destination must not exist")
    destination.mkdir(parents=True)
    _check_links(destination)
    manifest = {"version": 1, "suite_id": uuid.uuid4().hex, "created": datetime.now(timezone.utc).isoformat(),
        "agent_version": __import__("mini_agent").__version__, "provenance": "newly_authored_candidate_suite",
        "third_party_blind_acceptance": False, "core_fingerprint": core_fingerprint(), "cases": []}
    for definition in CASES:
        case = {key: definition[key] for key in ("id", "task", "category", "fault", "line")}
        project = destination / "projects" / case["id"]
        project.mkdir(parents=True)
        files = {**definition["files"], "tests/conftest.py": CONFTEXT,
            "pytest.ini": "[pytest]\ntestpaths = tests\n"}
        for relative, content in files.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        case["initial_hashes"] = {name: digest(project / name) for name in sorted(files)}
        if definition["oracle"]:
            oracle = destination / "oracles" / (case["id"] + ".py")
            oracle.parent.mkdir(exist_ok=True)
            oracle.write_text(definition["oracle"], encoding="utf-8")
            case["oracle_sha256"] = digest(oracle)
        case["interpreter"] = None
        if interpreter:
            trusted = validate_interpreter(Path(interpreter))
            dependency = subprocess.run([str(trusted), "-c", "import pathlib, pytest; print(pathlib.Path(pytest.__file__).parent.parent)"],
                capture_output=True, text=True, timeout=30)
            if dependency.returncode:
                raise ValueError("Trusted interpreter must already have pytest; dependencies are not installed")
            subprocess.run([str(trusted), "-m", "venv", "--without-pip", "--system-site-packages", str(project / ".venv")],
                check=True, capture_output=True, timeout=60)
            environment = project / ".venv"
            selected = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            site = next(environment.glob("Lib/site-packages")) if os.name == "nt" else next(environment.glob("lib/python*/site-packages"))
            (site / "acceptance-pytest.pth").write_text(dependency.stdout.strip() + "\n", encoding="utf-8")
            case["interpreter"] = str(validate_interpreter(selected))
        manifest["cases"].append(case)
    manifest["frozen_sha256"] = hashlib.sha256(json.dumps(manifest["cases"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    save(destination / "suite.json", manifest)
    return manifest


def suite(root):
    root = Path(root).absolute()
    _check_links(root)
    path = root / "suite.json"
    _check_links(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Suite manifest exceeds limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("cases"), list) or not 1 <= len(data["cases"]) <= 100:
        raise ValueError("Invalid suite manifest")
    actual = hashlib.sha256(json.dumps(data["cases"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if actual != data.get("frozen_sha256"):
        raise ValueError("Suite metadata changed after freezing")
    for case in data["cases"]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", case["id"]):
            raise ValueError("Invalid case ID")
        project = PolicyGuard(root / "projects" / case["id"])
        for name in case["initial_hashes"]:
            project.resolve(name, allow_missing=True)
        if case.get("oracle_sha256"):
            oracle = root / "oracles" / (case["id"] + ".py")
            _check_links(oracle)
            if digest(oracle) != case["oracle_sha256"]:
                raise ValueError("Hidden oracle changed")
    return data


def unchanged(root, case):
    policy = PolicyGuard(Path(root) / "projects" / case["id"])
    inventory = FileTools(policy, AgentConfig()).list_files()
    return (inventory.ok and not inventory.truncated and set(inventory.data["files"]) == set(case["initial_hashes"])
        and all(path.exists() and digest(path) == expected for name, expected in case["initial_hashes"].items()
               for path in [policy.resolve(name, allow_missing=True)])
        )


def oracle_test(root, case, label, *, config=None):
    """Oracle checks run only on a separate bounded copy, never on the model project."""
    root = Path(root)
    if label not in {"before", "after"}:
        raise ValueError("Invalid evaluation phase")
    run = root / "evaluation" / case["id"] / (label + "-" + uuid.uuid4().hex[:8])
    project = run / "project"
    project.mkdir(parents=True)
    source = PolicyGuard(root / "projects" / case["id"])
    config = config or AgentConfig()
    files = FileTools(source, config)
    selected = files.list_files(".")
    if not selected.ok or selected.truncated:
        raise ValueError("Evaluation copy inventory incomplete")
    total = 0
    for name in selected.data["files"]:
        raw, _ = files._read(name)
        total += len(raw)
        if total > config.max_validation_copy_bytes:
            raise ValueError("Evaluation copy exceeds budget")
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    oracle = root / "oracles" / (case["id"] + ".py")
    if digest(oracle) != case["oracle_sha256"]:
        raise ValueError("Oracle integrity check failed")
    target = project / "tests/test_acceptance_oracle.py"
    if target.exists():
        raise ValueError("Reserved oracle name exists in model project")
    target.write_bytes(oracle.read_bytes())
    # Reuse the frozen trusted bootstrap; a model must not redefine oracle imports.
    if digest(project / "tests/conftest.py") != case["initial_hashes"]["tests/conftest.py"]:
        raise ValueError("Test bootstrap changed")
    result = TestRunner(PolicyGuard(project), validate_interpreter(Path(case["interpreter"])), config, authorized=True).run(
        run / "results.json", targets=["tests/test_acceptance_oracle.py"] if label == "before" else ["tests"])
    save(run / "execution.json", result.to_dict())
    (run / "output.txt").write_text(result.output, encoding="utf-8")
    return {**result.to_dict(), "artifact": str(run / "execution.json")}


def location_match(diagnosis, case):
    """Evidence location is a proxy; never report it as semantic diagnosis correctness."""
    root = diagnosis.get("summary", {}).get("root_cause") or diagnosis.get("root_cause")
    if not root or root.get("status") not in {"evidence_supported", "supported_before_edit"}:
        return False
    ids = set(root.get("evidence_ids", []))
    return any(item["id"] in ids and item.get("kind") == "code" and item.get("path") == case["fault"]
               and item.get("revision") == 0 and item.get("line", 0) <= case["line"] <= item.get("end_line", 0)
               for item in diagnosis.get("evidence", []))


def score(root, case, audit, elapsed, interventions, before, *, backend):
    summary = audit.read_json("summary.json")
    diagnosis = audit.read_json("diagnosis.json")
    comparison = summary.get("comparison") or {}
    eligible = case["category"] != "missing_dependency"
    evaluation_error = None
    try:
        after = oracle_test(root, case, "after") if eligible else None
    except (OSError, ValueError, RuntimeError) as exc:
        evaluation_error = getattr(exc, "code", type(exc).__name__)
        after = {"passed": False, "complete": False, "error": evaluation_error}
    valid_baseline = bool(before and before.get("complete") and before.get("exit_code") == 1 and not before.get("collection_errors"))
    verified = summary["status"] == "verified"
    regression = bool(comparison.get("new_failures") or comparison.get("missing_cases") or comparison.get("newly_skipped"))
    project = Path(root) / "projects" / case["id"]
    public_tests_changed = any(not (project / name).exists() or digest(project / name) != expected
        for name, expected in case["initial_hashes"].items() if name.startswith("tests/") and name.endswith(".py"))
    costs = summary.get("api_cost")
    return {"case_id": case["id"], "category": case["category"], "backend": backend,
        "run_id": audit.run_id, "artifact": str(audit.directory), "status": summary["status"], "reason": summary["reason"],
        "elapsed_seconds": round(elapsed, 3), "human_interventions": interventions,
        "oracle_before": before, "oracle_after": after, "oracle_baseline_valid": valid_baseline,
        "repair_eligible": eligible, "repair_success": bool(eligible and valid_baseline and verified and after["passed"] and after["complete"] and not regression and not public_tests_changed),
        "evidence_location_match": location_match(diagnosis, case) if eligible else None,
        "diagnosis_correct": None, "diagnosis_review": "pending_human_semantic_review",
        "regression_detected": regression, "public_tests_changed": public_tests_changed, "api_cost": costs, "cost_known": costs is not None,
        "evaluation_error": evaluation_error,
        "model_requests": len(costs.get("attempts", [])) if costs else 0,
        "token_usage": summary.get("token_usage"), "actions": summary["actions"]}


def aggregate(records, *, batch_authorizations=0):
    live = [row for row in records if row.get("backend") == "live_api" and row.get("repair_eligible") and row.get("model_requests", 0) > 0]
    reviewed = [row for row in live if type(row.get("diagnosis_correct")) is bool]
    api = [row for row in records if row.get("backend") == "live_api"]
    costs = [row["api_cost"] for row in api if row.get("api_cost") is not None]
    return {"live_repair_cases": len(live), "repair_success_rate": sum(row["repair_success"] for row in live) / len(live) if live else None,
        "diagnosis_reviewed_cases": len(reviewed),
        "diagnosis_accuracy": sum(row["diagnosis_correct"] for row in reviewed) / len(reviewed) if reviewed else None,
        "evidence_location_match_rate": sum(row["evidence_location_match"] for row in live) / len(live) if live else None,
        "offline_cases": sum(row.get("backend") == "offline_invariant" for row in records),
        "live_attempts_without_model_request": sum(row.get("backend") == "live_api" and not row.get("model_requests") for row in records),
        "elapsed_seconds": round(sum(row.get("elapsed_seconds", 0) for row in api), 3),
        "human_interventions": sum(row.get("human_interventions", 0) for row in api) + batch_authorizations,
        "human_batch_authorizations": batch_authorizations,
        "total_accounted_upper_estimate_cny": str(sum((Decimal(cost["accounted_upper_estimate_cny"]) for cost in costs), Decimal(0))) if api and len(costs) == len(api) else None,
        "cost_unknown_cases": len(api) - len(costs),
        "assessment": "Candidate suite only; offline probes are excluded from live model rates."}


def apply_reviews(records, reviews):
    """Human semantic verdicts must identify an exact recorded run, with attribution."""
    records = [dict(row) for row in records]
    by_run = {(row["case_id"], row["run_id"]): row for row in records}
    seen = set()
    if not isinstance(reviews, list):
        raise ValueError("Reviews must be a list")
    for review in reviews:
        key = (review.get("case_id"), review.get("run_id"))
        if key not in by_run or key in seen or type(review.get("diagnosis_correct")) is not bool or any(
                not isinstance(review.get(field), str) or not review[field].strip() for field in ("reviewer", "note")):
            raise ValueError("Review requires a unique matching case/run, boolean verdict, reviewer and note")
        seen.add(key)
        by_run[key].update(diagnosis_correct=review["diagnosis_correct"], diagnosis_review="human_reviewed",
                           reviewer=review["reviewer"], review_note=review["note"])
    return records


def consent_handler(root, manifest, config_path, consent_path):
    """An explicit local authorization record binds this exact suite and config."""
    consent = json.loads(Path(consent_path).read_text(encoding="utf-8"))
    if (consent.get("suite_id") != manifest["suite_id"] or consent.get("frozen_sha256") != manifest["frozen_sha256"]
            or consent.get("config_sha256") != digest(Path(config_path)) or consent.get("allow_disclosure") is not True
            or consent.get("allow_tests") is not True or consent.get("allow_guarded_patches") is not True
            or not isinstance(consent.get("approved_by"), str) or not consent["approved_by"].strip()):
        raise ValueError("Consent does not authorize this exact frozen suite and configuration")
    def approve(case, kind, payload):
        if kind == "disclosure":
            return set(payload["files"]) == set(case["initial_hashes"])
        return kind == "proposal"  # Test expectation changes still require an independent human decision.
    return approve


def execution_plan(root, config_path):
    root = Path(root).absolute()
    manifest, config = suite(root), load_config(config_path)
    if config.api is None:
        raise ValueError("API configuration is required")
    return {"suite_id": manifest["suite_id"], "frozen_sha256": manifest["frozen_sha256"],
        "core_fingerprint": manifest["core_fingerprint"], "config_sha256": digest(Path(config_path)),
        "endpoint": config.api.endpoint, "protocol": config.api.protocol, "model": config.api.model,
        "per_case_limit_cny": config.api.max_run_cost_cny,
        "suite_limit_cny": config.api.max_run_cost_cny * len(manifest["cases"]),
        "notice": "Client estimates from configured prices, not a verified provider bill. Hidden oracles are excluded.",
        "cases": [{"case_id": case["id"], "project": str(root / "projects" / case["id"]), "task": case["task"],
                   "send_files": sorted(case["initial_hashes"])} for case in manifest["cases"]]}


def run_live(root, config_path, *, consent_path=None):
    """Interactive approvals preserve core disclosure and patch execution controls."""
    from .cli import build_parser
    from .panel import PanelInteraction
    from .runtime import run_task
    root = Path(root).absolute()
    manifest = suite(root)
    if manifest["core_fingerprint"] != core_fingerprint():
        raise ValueError("Core changed since suite preparation; prepare a new suite")
    if (root / "live-session.json").exists():
        raise ValueError("Live attempts are immutable; prepare a new suite for another attempt")
    config = load_config(config_path)
    if config.api is None:
        raise ValueError("A real API configuration is required")
    handler = consent_handler(root, manifest, config_path, consent_path) if consent_path else None
    save(root / "live-session.json", {"suite_id": manifest["suite_id"], "state": "started", "endpoint": config.api.endpoint,
        "model": config.api.model, "per_case_limit_cny": config.api.max_run_cost_cny,
        "suite_limit_cny": config.api.max_run_cost_cny * len(manifest["cases"]), "core_fingerprint": manifest["core_fingerprint"]})
    records = []
    for case in manifest["cases"]:
        if not case["interpreter"]:
            raise ValueError("Prepare with a trusted --test-python interpreter first")
        if not unchanged(root, case):
            raise ValueError("Case project changed; prepare a new suite for another attempt")
        before = oracle_test(root, case, "before") if case.get("oracle_sha256") else None
        if before and (not before["complete"] or before["exit_code"] != 1 or before["collection_errors"]):
            raise ValueError("Hidden baseline did not reproduce a complete failure; no model request sent")
        project = root / "projects" / case["id"]
        runs = root / "runs" / case["id"]
        class Interaction(PanelInteraction):
            def __init__(self):
                import io
                super().__init__(io.StringIO(), io.StringIO())
                self.interventions = 0
                self.approval_requests = 0
                self.automatic_approvals = 0
            def emit(self, kind, **data):
                if kind == "display":
                    print(data["text"])
            def request(self, kind, payload):
                self.approval_requests += 1
                self.audit.record("acceptance_approval_requested", kind=kind, payload=payload)
                print(json.dumps({"case": case["id"], "kind": kind, "payload": payload}, ensure_ascii=False, indent=2))
                if handler:
                    approved = handler(case, kind, payload)
                    self.automatic_approvals += int(approved)
                else:
                    self.interventions += 1
                    try:
                        approved = input("批准此范围/提案？[y/N]: ").strip().lower() == "y"
                    except (EOFError, KeyboardInterrupt):
                        self._cancelled.set()
                        approved = False
                self.audit.record("acceptance_approval_resolved", kind=kind, approved=approved,
                                  mode="frozen_suite_consent" if handler else "human_interactive")
                return approved
        args = build_parser().parse_args(["run", "--project", str(project), "--task", case["task"], "--model", "api",
            "--allow-tests", "--config", str(config_path), "--runs-dir", str(runs)])
        interaction = Interaction()
        start = time.monotonic()
        run_task(args, interaction=interaction)
        if not hasattr(interaction, "audit") or not interaction.audit._path("summary.json").exists():
            raise ValueError("Run produced no complete summary; inspect artifacts before retry")
        row = score(root, case, interaction.audit, time.monotonic() - start, interaction.interventions, before, backend="live_api")
        row.update(approval_requests=interaction.approval_requests, automatic_approvals=interaction.automatic_approvals)
        records.append(row)
        save(root / "live-results.json", {"suite_id": manifest["suite_id"], "records": records, "metrics": aggregate(records, batch_authorizations=1 if handler else 0),
            "human_batch_authorizations": 1 if handler else 0})
        if interaction.cancelled():
            break
    session = json.loads((root / "live-session.json").read_text(encoding="utf-8"))
    session.update(state="cancelled" if interaction.cancelled() else "completed", completed_cases=len(records))
    save(root / "live-session.json", session)
    return {"records": records, "metrics": aggregate(records, batch_authorizations=1 if handler else 0)}


def main(argv=None):
    # Windows redirected streams may default to GBK, which cannot encode the CNY sign.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Independent candidate acceptance tooling; no scripted model success claims.")
    commands = parser.add_subparsers(dest="operation", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--destination", type=Path, required=True)
    prep.add_argument("--test-python", type=Path, required=True)
    live = commands.add_parser("live")
    live.add_argument("--suite", type=Path, required=True)
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--consent", type=Path, help="Explicit authorization for this exact suite/config; never inferred from credentials")
    plan = commands.add_parser("plan")
    plan.add_argument("--suite", type=Path, required=True)
    plan.add_argument("--config", type=Path, required=True)
    offline = commands.add_parser("offline")
    offline.add_argument("--suite", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--suite", type=Path, required=True)
    report.add_argument("--reviews", type=Path, help="Optional human semantic verdicts; original run records remain unchanged")
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            result = prepare(args.destination, interpreter=args.test_python)
        elif args.operation == "live":
            result = run_live(args.suite, args.config, consent_path=args.consent)
        elif args.operation == "plan":
            result = execution_plan(args.suite, args.config)
        elif args.operation == "offline":
            from .acceptance_probes import run_offline
            result = run_offline(args.suite)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        else:
            suite(args.suite)
            live_data = json.loads((args.suite / "live-results.json").read_text(encoding="utf-8")) if (args.suite / "live-results.json").exists() else {}
            records = live_data.get("records", [])
            if args.reviews:
                records = apply_reviews(records, json.loads(args.reviews.read_text(encoding="utf-8")))
            offline_path = args.suite / "offline-results.json"
            offline_result = json.loads(offline_path.read_text(encoding="utf-8")) if offline_path.exists() else None
            result = {"records": records, "metrics": aggregate(records + (offline_result or {}).get("records", []),
                batch_authorizations=live_data.get("human_batch_authorizations", 0)), "offline": offline_result}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.operation == "live":
            return 0 if result["records"] and all(
                row["repair_success"] if row["repair_eligible"] else row["reason"] == "test_dependency_unavailable"
                for row in result["records"]) else 1
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
