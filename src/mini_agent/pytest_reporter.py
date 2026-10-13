"""Loaded only in the explicitly authorized pytest process; bounded metadata, no output."""

import json
import os
from pathlib import Path


_cases = {}
_collection_errors = []
_incomplete = False
_size = 0


def pytest_runtest_logreport(report):
    global _incomplete, _size
    name = report.nodeid
    if len(name) > 512 or (name not in _cases and (len(_cases) >= 1000 or _size + len(name) > 100000)):
        _incomplete = True
        return
    if name not in _cases:
        _size += len(name)
    if report.failed:
        _cases[name] = "failed" if report.when == "call" else "error"
    elif report.skipped and _cases.get(name) not in {"failed", "error"}:
        _cases[name] = "skipped"
    elif report.when == "call" and _cases.get(name) not in {"failed", "error", "skipped"}:
        _cases[name] = "passed"


def pytest_collectreport(report):
    global _incomplete
    if report.failed:
        if len(_collection_errors) >= 100 or len(report.nodeid) > 512:
            _incomplete = True
        else:
            _collection_errors.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    payload = {"version": 1, "exit_code": int(exitstatus), "complete": not _incomplete,
               "cases": _cases, "collection_errors": _collection_errors}
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    if len(raw) > 256 * 1024:
        raw = json.dumps({**payload, "complete": False, "cases": {}, "collection_errors": []}).encode("utf-8")
    Path(os.environ["MINI_AGENT_TEST_REPORT"]).write_bytes(raw)
