"""Acceptance scoring must not confuse oracle proof or scripted probes with model skill."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from mini_agent.acceptance import prepare, suite, oracle_test, unchanged, aggregate, location_match, run_live, score, save, apply_reviews, execution_plan, consent_handler
from mini_agent.acceptance_probes import run_offline
from mini_agent.audit import AuditLog


@pytest.fixture(scope="module")
def prepared(tmp_path_factory, interpreter):
    root = tmp_path_factory.mktemp("acceptance") / "suite"
    data = prepare(root, interpreter=interpreter)
    return root, data


def test_frozen_cases_exclude_oracles_and_location_hints(prepared):
    root, data = prepared
    for case in data["cases"]:
        project = root / "projects" / case["id"]
        assert unchanged(root, case)
        assert not (project / "tests/test_acceptance_oracle.py").exists()
        assert "src/" not in case["task"] and ".py" not in case["task"]
        assert Path(case["interpreter"]).is_relative_to(project / ".venv")
    assert suite(root)["frozen_sha256"] == data["frozen_sha256"]
    with pytest.raises(ValueError, match="must not exist"):
        prepare(root)


def test_oracle_and_manifest_tampering_are_rejected(tmp_path):
    root = tmp_path / "suite"
    data = prepare(root)
    oracle = root / "oracles/batch_total.py"
    original = oracle.read_bytes()
    oracle.write_bytes(b"def test_false_positive(): pass\n")
    with pytest.raises(ValueError, match="oracle changed"):
        suite(root)
    oracle.write_bytes(original)
    data["cases"][0]["task"] = "Hints changed after freeze"
    save(root / "suite.json", data)
    with pytest.raises(ValueError, match="metadata changed"):
        suite(root)


def test_oracles_reject_defects_and_preserve_model_projects(prepared):
    root, data = prepared
    for case in data["cases"]:
        if case.get("oracle_sha256"):
            result = oracle_test(root, case, "before")
            assert result["exit_code"] == 1 and result["complete"]
            assert not result["collection_errors"]
            assert unchanged(root, case)


def test_candidate_gold_oracles_accept_correct_semantics(tmp_path, interpreter):
    root = tmp_path / "suite"
    data = prepare(root, interpreter=interpreter)
    fixes = {"batch_total": ("sum(rows[:-1])", "sum(rows)"),
        "interval_overlap": ("min(start, left)", "max(start, left)"),
        "dynamic_prefix": ("text[:-len(prefix)]", "text[len(prefix):]")}
    for case in data["cases"]:
        if case["id"] not in fixes:
            continue
        source = root / "projects" / case["id"] / case["fault"]
        old, new = fixes[case["id"]]
        source.write_text(source.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        result = oracle_test(root, case, "after")
        assert result["passed"] and result["complete"]
        assert not unchanged(root, case)


def test_offline_reliability_cases_have_no_live_success_rate(prepared):
    root, _ = prepared
    result = run_offline(root)
    assert result["passed"], result
    assert len(result["records"]) == 8
    assert len(result["oracle_baselines"]) == 3
    assert result["repair_success_rate"] is None and not result["live_model_evaluated"]
    assert aggregate(result["records"])["live_repair_cases"] == 0
    with pytest.raises(ValueError, match="immutable"):
        run_offline(root)


def test_aggregate_excludes_mock_denied_and_unreviewed_diagnosis():
    live = {"backend": "live_api", "repair_eligible": True, "model_requests": 2,
        "repair_success": True, "evidence_location_match": True, "diagnosis_correct": None}
    rows = [live, {**live, "backend": "offline_invariant"}, {**live, "model_requests": 0},
        {**live, "repair_eligible": False}, {**live, "repair_success": False}]
    result = aggregate(rows)
    assert result["repair_success_rate"] == .5
    assert result["diagnosis_accuracy"] is None and result["diagnosis_reviewed_cases"] == 0
    assert result["live_repair_cases"] == 2 and result["live_attempts_without_model_request"] == 1
    reviewed = deepcopy(rows)
    reviewed[0]["diagnosis_correct"] = False
    assert aggregate(reviewed)["diagnosis_accuracy"] == 0


def test_location_match_is_citation_and_revision_aware():
    case = {"fault": "src/a.py", "line": 3}
    diagnosis = {"root_cause": {"status": "evidence_supported", "evidence_ids": ["E1"]},
        "evidence": [{"id": "E1", "kind": "code", "path": "src/a.py", "line": 2, "end_line": 5, "revision": 0}]}
    assert location_match(diagnosis, case)
    diagnosis["evidence"][0]["revision"] = 1
    assert not location_match(diagnosis, case)
    diagnosis["evidence"][0]["revision"] = 0
    diagnosis["summary"] = {"root_cause": {"status": "unconfirmed", "evidence_ids": ["E1"]}}
    assert not location_match(diagnosis, case)


def test_live_rejects_changed_core_without_reading_credentials_or_network(tmp_path):
    root = tmp_path / "suite"
    data = prepare(root)
    data["core_fingerprint"] = "different"
    save(root / "suite.json", data)
    with pytest.raises(ValueError, match="Core changed"):
        run_live(root, tmp_path / "missing-config.json")


def test_extra_hint_file_invalidates_fresh_attempt(tmp_path):
    root = tmp_path / "suite"
    data = prepare(root)
    case = data["cases"][0]
    assert unchanged(root, case)
    (root / "projects" / case["id"] / "answer.txt").write_text("An answer accidentally disclosed")
    assert not unchanged(root, case)


def test_human_reviews_require_exact_run_and_attribution():
    records = [{"case_id": "a", "run_id": "run-a", "diagnosis_correct": None}]
    review = {"case_id": "a", "run_id": "run-a", "diagnosis_correct": True, "reviewer": "tester", "note": "Cause matches the trigger and evidence"}
    assert apply_reviews(records, [review])[0]["diagnosis_correct"] is True
    assert records[0]["diagnosis_correct"] is None
    for bad in ({**review, "run_id": "other"}, {**review, "note": ""}, {**review, "diagnosis_correct": "true"}):
        with pytest.raises(ValueError):
            apply_reviews(records, [bad])
    with pytest.raises(ValueError):
        apply_reviews(records, [review, review])


@pytest.mark.parametrize("verified,oracle_pass,changed,expected", [
    (True, False, False, False), (False, True, False, False),
    (True, True, True, False), (True, True, False, True)])
def test_score_needs_independent_oracle_and_unchanged_public_tests(tmp_path, monkeypatch, verified, oracle_pass, changed, expected):
    root = tmp_path / "suite"
    data = prepare(root)
    case = data["cases"][0]
    project = root / "projects" / case["id"]
    audit = AuditLog(tmp_path / "runs", project)
    audit.write_json("summary.json", {"status": "verified" if verified else "unverified", "reason": "test",
        "comparison": {"new_failures": [], "missing_cases": []}, "actions": 4, "api_cost": None})
    audit.write_json("diagnosis.json", {"root_cause": None})
    monkeypatch.setattr("mini_agent.acceptance.oracle_test", lambda *a: {"passed": oracle_pass, "complete": True})
    if changed:
        (project / "tests/test_total.py").write_text("def test_weakened(): pass\n")
    row = score(root, case, audit, 1.2, 2, {"exit_code": 1, "complete": True, "collection_errors": []}, backend="live_api")
    assert row["repair_success"] is expected
    assert row["diagnosis_correct"] is None
    assert not row["cost_known"]


def test_oracle_execution_error_is_a_recorded_failure(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    case = prepare(root)["cases"][0]
    audit = AuditLog(tmp_path / "runs", root / "projects" / case["id"])
    audit.write_json("summary.json", {"status": "verified", "reason": "test", "actions": 2})
    audit.write_json("diagnosis.json", {"root_cause": None})
    def fail(*args):
        raise ValueError("Bootstrap changed")
    monkeypatch.setattr("mini_agent.acceptance.oracle_test", fail)
    row = score(root, case, audit, 0, 0, {"complete": True, "exit_code": 1}, backend="live_api")
    assert not row["repair_success"] and row["evaluation_error"] == "ValueError"


def test_execution_plan_and_consent_bind_scope_and_config(tmp_path):
    root = tmp_path / "suite"
    data = prepare(root)
    config = tmp_path / "api.json"
    save(config, {"api": {"endpoint": "http://127.0.0.1/unused", "model": "test", "protocol": "chat_completions"}})
    plan = execution_plan(root, config)
    assert plan["suite_limit_cny"] == 2
    assert len(plan["cases"]) == 4
    assert all("oracle" not in name for case in plan["cases"] for name in case["send_files"])
    consent = {key: plan[key] for key in ("suite_id", "frozen_sha256", "config_sha256")}
    consent.update(allow_disclosure=True, allow_tests=True, allow_guarded_patches=True, approved_by="test")
    path = root / "test-consent.json"
    save(path, consent)
    handler = consent_handler(root, data, config, path)
    case = data["cases"][0]
    assert handler(case, "disclosure", {"files": list(case["initial_hashes"])})
    assert not handler(case, "disclosure", {"files": ["../oracles/batch_total.py"]})
    assert handler(case, "proposal", {})
    assert not handler(case, "test_expectation", {})
    config.write_text(config.read_text() + "\n")
    with pytest.raises(ValueError, match="Consent"):
        consent_handler(root, data, config, path)


def test_live_does_not_rerun_or_overwrite_previous_attempt(tmp_path):
    root = tmp_path / "suite"
    prepare(root)
    save(root / "live-session.json", {"state": "started"})
    with pytest.raises(ValueError, match="immutable"):
        run_live(root, tmp_path / "missing.json")


def test_entrypoint_reconfigures_redirected_windows_output(monkeypatch):
    import io
    from mini_agent.acceptance import main
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr("mini_agent.acceptance.prepare", lambda *a, **k: {"task": "合成验收 ¥"})
    assert main(["prepare", "--destination", "unused", "--test-python", "unused"]) == 0
    stream.flush()
    assert "合成验收 ¥" in buffer.getvalue().decode("utf-8")


def test_live_command_failure_does_not_exit_success(monkeypatch):
    from mini_agent.acceptance import main
    monkeypatch.setattr("mini_agent.acceptance.run_live", lambda *a, **k: {"records": [
        {"repair_eligible": True, "repair_success": False}]})
    assert main(["live", "--suite", "unused", "--config", "unused"]) == 1
