"""Development-only check: the example must have exactly the intended failure."""

from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mini-agent-baseline-") as directory:
        report = Path(directory) / "baseline.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={report}"],
            cwd=root / "examples" / "buggy_project",
            timeout=60,
            check=False,
        )
        if result.returncode != 1 or not report.is_file():
            print("Unexpected example exit code or missing test report.", file=sys.stderr)
            return 1
        cases = ET.parse(report).findall(".//testcase")
        outcomes = {case.get("name"): case for case in cases}
        if len(cases) != 2 or set(outcomes) != {"test_average_empty", "test_average_non_empty"}:
            print("Unexpected example test set.", file=sys.stderr)
            return 1
        empty, normal = outcomes["test_average_empty"], outcomes["test_average_non_empty"]
        failure = empty.find("failure")
        if (failure is None or "ZeroDivisionError" not in (failure.text or "")
                or empty.find("error") is not None or empty.find("skipped") is not None
                or any(normal.find(tag) is not None for tag in ("failure", "error", "skipped"))):
            print("Example failed for an unexpected reason.", file=sys.stderr)
            return 1
    print("Expected baseline verified: 1 failed (empty list), 1 passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
