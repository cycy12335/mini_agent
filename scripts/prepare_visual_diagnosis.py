"""Generate an offline, read-only diagnosis artifact for visual UI-4 acceptance."""
import json
from pathlib import Path
import uuid

from check_installed_diagnosis import ScriptedDiagnosis
from mini_agent.audit import AuditLog
from mini_agent.config import AgentConfig
from mini_agent.controller import AgentController
from mini_agent.policy import PolicyGuard
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry


def main():
    repo = Path(__file__).resolve().parents[1]
    key = uuid.uuid4().hex[:12]
    project = repo / "examples" / f"demo-visual-diagnosis-{key}"
    (project / "src/shop").mkdir(parents=True)
    (project / "src/shop/entry.py").write_text("from .maths import mean as evaluate\ndef checkout(values): return evaluate(values)\n", encoding="utf-8")
    (project / "src/shop/maths.py").write_text("def mean(values):\n    return sum(values) / len(values)\n", encoding="utf-8")
    config = AgentConfig()
    audit = AuditLog(repo / ".mini-agent/visual-diagnosis/runs", project)
    result = AgentController(ScriptedDiagnosis(), ToolRegistry(FileTools(PolicyGuard(project), config)),
                             audit, config, read_only=True).run("checkout([]) should return 0")
    assert result.status == "proposed"
    print("Offline read-only diagnosis sample ready. No model API was called.")
    print("Import this run folder into the Mini Agent panel:")
    print(audit.directory)
    (repo / ".mini-agent/visual-diagnosis/latest.json").write_text(json.dumps({"directory": str(audit.directory)}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
