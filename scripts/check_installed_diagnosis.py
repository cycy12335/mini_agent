"""Offline installed-wheel smoke check for cross-file diagnostic evidence."""

import json
from pathlib import Path
import sys
import uuid

import mini_agent
from mini_agent.adapters.mock import call
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.policy import PolicyGuard
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry


class ScriptedDiagnosis:
    def __init__(self):
        self.step = 0

    def next_action(self, messages, schemas):
        self.step += 1
        data = next((item["result"]["data"] for item in reversed(messages)
                     if item.get("role") == "tool"), {})
        if self.step == 1:
            return call("record_problem", expected_behavior="checkout([]) returns 0", trigger="Empty input",
                        observed_behavior="ZeroDivisionError")
        if self.step == 2:
            return call("find_definition", symbol="checkout")
        if self.step == 3:
            return call("find_callees", symbol="checkout")
        if self.step == 4:
            return call("find_definition", symbol=data["matches"][0]["targets"][0])
        if self.step == 5:
            self.path = data["matches"][0]["path"]
            return call("propose_hypothesis", claim="Empty values produce a zero denominator",
                        evidence_ids=[data["evidence"][0]["id"]])
        if self.step == 6:
            self.hypothesis = data["hypothesis"]["id"]
            return call("read_file", path=self.path)
        if self.step == 7:
            return call("record_check", hypothesis_id=self.hypothesis, outcome="supported",
                        finding="Source divides by len(values) without a guard",
                        evidence_ids=[data["evidence"][0]["id"]])
        if self.step == 8:
            return call("conclude_root_cause", hypothesis_id=self.hypothesis,
                        conclusion="Empty input reaches an unguarded zero denominator")
        return {"type": "final", "message": "Source supports a zero denominator; execution and repair unverified."}


def main():
    if not Path(mini_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Run against the installed wheel with PYTHONPATH cleared")
    workspace = Path(__file__).resolve().parents[1]
    identifier = uuid.uuid4().hex[:12]
    root = workspace / ".pytest-tmp" / ("installed-core3-" + identifier)
    (root / "src/shop").mkdir(parents=True)
    (root / "src/shop/entry.py").write_text("from .maths import mean as evaluate\ndef checkout(values): return evaluate(values)\n", encoding="utf-8")
    (root / "src/shop/maths.py").write_text("def mean(values):\n    return sum(values) / len(values)\n", encoding="utf-8")
    config = AgentConfig()
    audit = AuditLog(workspace / ".mini-agent/installed-core3" / identifier, root)
    result = AgentController(ScriptedDiagnosis(), ToolRegistry(FileTools(PolicyGuard(root), config)),
                             audit, config, read_only=True).run("checkout([]) should return 0")
    assert result.status == "proposed"
    state = audit.read_json("diagnosis.json")
    assert state["summary"]["status"] == "evidence_supported"
    assert any(item["path"] == "src/shop/maths.py" for item in state["evidence"])
    print(json.dumps({"version": mini_agent.__version__, "installed": mini_agent.__file__,
                      "diagnosis": state["summary"]["status"], "artifact": str(audit.directory / "diagnosis.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
