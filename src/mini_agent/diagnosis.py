"""Visible diagnostic claims backed by controller-issued, versioned evidence."""

from copy import deepcopy
import hashlib
import json

from .actions import validate_action
from .models import ToolError, ToolResult
from .privacy import Redactor, ScopedFiles
from .tools.files import FileTools, result_boundary


class Diagnosis:
    def __init__(self, files, config, task):
        self.files, self.config = files, config
        self.reader = ScopedFiles(files.policy, config, files.allowed, files.redactor) if isinstance(files, ScopedFiles) else FileTools(files.policy, config)
        self.redactor = getattr(files, "redactor", None) or Redactor()
        self.state = {"version": 1, "task": task, "problem": None, "hypotheses": [],
                      "evidence": [], "root_cause": None, "evidence_truncated": False,
                      "assessment": "Model claims with validated evidence citations; semantic causality is not independently proven."}
        self.sequence = 0

    def _fits(self, state):
        return len(json.dumps(state, ensure_ascii=False)) <= self.config.max_diagnosis_chars

    def _commit(self, state):
        if not self._fits(state):
            raise ToolError("DIAGNOSIS_LIMIT", "Structured diagnosis exceeds storage budget")
        self.state = state

    def _add_evidence(self, item):
        if len(self.state["evidence"]) >= self.config.max_evidence:
            self.state["evidence_truncated"] = True
            return None
        self.sequence += 1
        item = {"id": f"E{self.sequence:04}", "sequence": self.sequence, **item}
        candidate = deepcopy(self.state)
        candidate["evidence"].append(item)
        if not self._fits(candidate):
            self.state["evidence_truncated"] = True
            return None
        self.state = candidate
        return item

    def source(self, path, line, end_line, digest, *, revision, tool, uncertain=False, symbol=None, expected_text=None):
        try:
            raw, text = self.reader._read(path)
            if self.redactor.text(text) != text:
                return None
            actual = hashlib.sha256(raw).hexdigest()
            if digest is not None and actual != digest:
                return None
            lines = text.splitlines()
            if type(line) is not int or type(end_line) is not int or not 1 <= line <= end_line <= max(1, len(lines)):
                return None
            if expected_text is not None and (not lines or expected_text not in lines[line - 1]):
                return None
            return self._add_evidence({"kind": "code", "tool": tool, "revision": revision,
                                       "path": path, "line": line, "end_line": end_line,
                                       "sha256": actual, "symbol": symbol, "uncertain": uncertain,
                                       "excerpt": "\n".join(lines[line - 1:end_line])[:500]})
        except (ToolError, OSError):
            return None

    def observe(self, name, result, revision):
        if not result.ok or not isinstance(result.data, dict):
            return result
        data = dict(result.data)
        candidates = []
        if name == "read_file":
            candidates = [{"path": data["path"], "line": 1,
                           "end_line": max(1, len(data["content"].splitlines())), "sha256": data["sha256"]}]
        elif name == "search_text":
            candidates = data.get("matches", [])
        elif name.startswith("find_"):
            candidates = data.get("matches", [])
        issued = []
        for candidate in candidates:
            if "path" not in candidate or "line" not in candidate:
                continue
            coverage = data.get("coverage", {})
            item = self.source(candidate["path"], candidate["line"],
                               candidate.get("end_line", candidate["line"]), candidate.get("sha256"),
                               revision=revision, tool=name,
                               uncertain=candidate.get("uncertain", False) or candidate.get("conditional", False)
                               or candidate.get("decorated", False) or coverage.get("complete") is False,
                               symbol=candidate.get("symbol") or candidate.get("expression"),
                               expected_text=candidate.get("text"))
            if item:
                issued.append({key: item[key] for key in ("id", "path", "line", "end_line", "sha256", "uncertain")})
            if len(issued) >= 16:
                break
        if name in {"read_file", "search_text"} or name.startswith("find_"):
            data["evidence"] = issued
            data["evidence_truncated"] = self.state["evidence_truncated"] or len(candidates) > len(issued)
            # Evidence cannot inflate a previously bounded tool response.
            while issued and len(json.dumps(data, ensure_ascii=False)) > self.config.max_tool_output_chars:
                issued.pop()
                data["evidence_truncated"] = True
            return ToolResult(result.ok, data, result.error_code, result.message, result.truncated)
        return result

    def snapshot(self):
        """Hash only readable disclosure-scope files, without adding source evidence."""
        names, truncated, _ = self.reader._scan(".")
        hashes, size = {}, 0
        complete = not truncated
        for name in names:
            try:
                raw, text = self.reader._read(name)
                if self.redactor.text(text) != text:
                    complete = False
                    continue
                size += len(raw)
                if size > self.config.max_index_bytes:
                    complete = False
                    break
                hashes[name] = hashlib.sha256(raw).hexdigest()
            except (ToolError, OSError):
                complete = False
        return {"hashes": hashes, "complete": complete}

    def observe_test(self, result, phase, revision, artifact, before, after):
        stable = before["complete"] and after["complete"] and before["hashes"] == after["hashes"]
        item = self._add_evidence({"kind": "execution", "tool": "run_tests", "phase": phase,
                                   "revision": revision, "artifact": artifact, "exit_code": result.exit_code,
                                   "passed": result.passed, "complete": result.complete,
                                   "case_count": len(result.cases or {}),
                                   "failures": [case for case, outcome in (result.cases or {}).items() if outcome in {"failed", "error"}][:8],
                                   "scope_hashes": before["hashes"], "scope": "readable_approved_files",
                                   "scope_complete": before["complete"],
                                   "uncertain": not stable or not result.complete or not result.cleanup_ok
                                   or result.exit_code not in {0, 1} or not result.cases
                                   or bool(result.error or result.timed_out or result.cancelled or result.collection_errors)})
        return item

    def _evidence(self, ids, *, require_reliable=False, after=None, revision=None):
        by_id = {item["id"]: item for item in self.state["evidence"]}
        if not ids:
            raise ToolError("EVIDENCE_REQUIRED", "Use controller-issued evidence IDs from source or execution results")
        found = []
        for identity in ids:
            if identity not in by_id:
                raise ToolError("UNKNOWN_EVIDENCE", "Evidence ID was not issued by this run")
            item = by_id[identity]
            if revision is not None and item["revision"] != revision:
                raise ToolError("STALE_EVIDENCE", "Evidence belongs to a previous source revision; inspect or execute again")
            if after is not None and item["sequence"] <= after:
                raise ToolError("CHECK_EVIDENCE_REQUIRED", "Perform a fresh source query or authorized test after proposing the hypothesis")
            try:
                expected = {item["path"]: item["sha256"]} if item["kind"] == "code" else item["scope_hashes"]
                for path, digest in expected.items():
                    raw, text = self.reader._read(path)
                    if self.redactor.text(text) != text or hashlib.sha256(raw).hexdigest() != digest:
                        raise ToolError("STALE_EVIDENCE", "Cited source changed or became sensitive; gather evidence again")
                if item["kind"] == "execution" and item["scope_complete"]:
                    names, truncated, _ = self.reader._scan(".")
                    if truncated or set(names) != set(expected):
                        raise ToolError("STALE_EVIDENCE", "Execution scope changed; run authorized tests again")
            except (OSError, ToolError) as exc:
                raise ToolError("STALE_EVIDENCE", "Evidence is no longer valid in the allowed file scope") from exc
            found.append(item)
        if require_reliable and not any(not item["uncertain"] for item in found):
            raise ToolError("UNCERTAIN_EVIDENCE", "Uncertain static candidates or incomplete execution cannot support a conclusive check")
        return found

    @result_boundary
    def execute(self, name, arguments, revision):
        validate_action({"type": "tool_call", "name": name, "arguments": arguments}, self.config)
        state = deepcopy(self.state)
        if name == "record_problem":
            if state["hypotheses"]:
                raise ToolError("PROBLEM_LOCKED", "Problem conditions are immutable after proposing a hypothesis; record limitations instead")
            state["problem"] = {**arguments, "assumptions": arguments.get("assumptions", [])}
            result = {"problem": state["problem"]}
        elif name == "propose_hypothesis":
            if state["problem"] is None:
                raise ToolError("PROBLEM_REQUIRED", "Record expected behavior, trigger and observed behavior first")
            if len(state["hypotheses"]) >= self.config.max_hypotheses:
                raise ToolError("HYPOTHESIS_LIMIT", "Hypothesis budget exhausted; report unresolved candidates")
            self._evidence(arguments["evidence_ids"], revision=revision)
            hypothesis = {"id": f"H{len(state['hypotheses']) + 1:04}", "claim": arguments["claim"],
                          "evidence_ids": arguments["evidence_ids"], "proposed_after": self.sequence,
                          "status": "pending", "checks": []}
            state["hypotheses"].append(hypothesis)
            result = {"hypothesis": hypothesis}
        elif name in {"record_check", "conclude_root_cause"}:
            hypothesis = next((item for item in state["hypotheses"] if item["id"] == arguments["hypothesis_id"]), None)
            if hypothesis is None:
                raise ToolError("UNKNOWN_HYPOTHESIS", "Hypothesis ID was not issued by this run")
            if name == "record_check":
                decisive = arguments["outcome"] in {"supported", "rejected"}
                if decisive or arguments["evidence_ids"]:
                    self._evidence(arguments["evidence_ids"], require_reliable=decisive,
                                   after=hypothesis["proposed_after"], revision=revision)
                check = {"outcome": arguments["outcome"], "finding": arguments["finding"],
                         "evidence_ids": arguments["evidence_ids"], "revision": revision}
                hypothesis["checks"].append(check)
                hypothesis["status"] = arguments["outcome"]
                if state["root_cause"] and state["root_cause"]["hypothesis_id"] == hypothesis["id"]:
                    state["root_cause"] = None
                result = {"hypothesis_id": hypothesis["id"], "check": check}
            else:
                if hypothesis["status"] != "supported":
                    raise ToolError("ROOT_CAUSE_UNSUPPORTED", "Root cause requires a supported hypothesis with a conclusive check")
                check = hypothesis["checks"][-1]
                self._evidence(hypothesis["evidence_ids"] + check["evidence_ids"], require_reliable=True, revision=revision)
                root = {"hypothesis_id": hypothesis["id"], "conclusion": arguments["conclusion"],
                        "evidence_ids": list(dict.fromkeys(hypothesis["evidence_ids"] + check["evidence_ids"])),
                        "revision": revision, "status": "evidence_supported",
                        "unresolved": arguments.get("unresolved", [])}
                state["root_cause"] = root
                result = {"root_cause": root}
        else:
            raise ToolError("UNKNOWN_DIAGNOSIS_TOOL", "Unsupported diagnosis operation")
        if len(json.dumps(result, ensure_ascii=False)) > self.config.max_tool_output_chars:
            raise ToolError("DIAGNOSIS_OUTPUT_LIMIT", "Diagnostic result exceeds the tool output budget")
        self._commit(state)
        return ToolResult(True, result, message="Diagnostic claim recorded with validated evidence; see diagnosis.json")

    def summary(self, revision, reason):
        root = deepcopy(self.state["root_cause"])
        unresolved = list(self.state["problem"].get("assumptions", [])) if self.state["problem"] else ["Expected behavior and trigger have not been structured."]
        if root:
            unresolved.extend(root["unresolved"])
            if root["revision"] < revision:
                root["status"] = "supported_before_edit"
            else:
                try:
                    self._evidence(root["evidence_ids"], revision=revision)
                except ToolError:
                    root["status"] = "unconfirmed"
                    unresolved.append("Root-cause evidence changed; the conclusion needs a fresh check.")
        for hypothesis in self.state["hypotheses"]:
            if hypothesis["status"] in {"pending", "inconclusive"}:
                unresolved.append(f"{hypothesis['id']}: {hypothesis['claim']}")
        if not root:
            unresolved.append("No evidence-supported root cause was established.")
        if self.state["evidence_truncated"]:
            unresolved.append("Evidence collection reached its storage budget.")
        return {"status": root["status"] if root else "unconfirmed", "root_cause": root,
                "unresolved": list(dict.fromkeys(unresolved)), "termination": reason,
                "hypotheses": len(self.state["hypotheses"]), "evidence": len(self.state["evidence"])}
