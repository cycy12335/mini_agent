"""Exercise installed acceptance tooling without a live model or network request."""
import json
from pathlib import Path
import sys
import uuid

import mini_agent
from mini_agent.acceptance import prepare, aggregate, unchanged, execution_plan
from mini_agent.acceptance_probes import run_offline


def main():
    if not Path(mini_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Run with installed wheel and PYTHONPATH cleared")
    workspace = Path(__file__).resolve().parents[1]
    root = workspace / ".pytest-tmp" / ("installed-acceptance-" + uuid.uuid4().hex[:12])
    manifest = prepare(root, interpreter=sys.executable)
    result = run_offline(root)
    assert result["passed"] and len(result["records"]) == 8
    assert aggregate(result["records"])["repair_success_rate"] is None
    assert all(unchanged(root, case) for case in manifest["cases"])
    plan = execution_plan(root, workspace / "agent.api.example.json")
    assert len(plan["cases"]) == 4 and plan["suite_limit_cny"] == 2
    print(json.dumps({"version": mini_agent.__version__, "installed": mini_agent.__file__,
        "offline_invariants": 8, "valid_oracle_baselines": 3, "live_model_evaluated": False,
        "artifact": str(root / "offline-results.json")}))


if __name__ == "__main__":
    main()
