"""Prepare disposable offline GUI fixtures; never touches the original example."""
import argparse
import json
from pathlib import Path
import shutil
import stat
import sys


def check_local_path(path):
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RuntimeError("Refusing a linked fixture path")


def main():
    parser = argparse.ArgumentParser(description="Prepare the VS Code offline manual test")
    parser.add_argument("--reset", action="store_true", help="Overwrite only fixture files in examples/demo-visual")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    origin = repo / "examples/buggy_project"
    destination = repo / "examples/demo-visual"
    check_local_path(destination)
    fixtures = ["src/calculator.py", "tests/test_calculator.py", "pytest.ini"]
    settings = {"miniAgent.pythonPath": sys.executable,
                "miniAgent.testPythonPath": sys.executable, "miniAgent.configPath": ""}
    for scenario in ("readonly", "repair", "cancel", "decline"):
        target = destination / scenario
        for relative in fixtures:
            output = target / relative
            check_local_path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            if args.reset or not output.exists():
                shutil.copyfile(origin / relative, output)
        config = target / ".vscode/settings.json"
        check_local_path(config)
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Offline manual-test fixtures ready. No API key required.")
    print(f"VS Code folder: {repo / 'extensions/vscode'}")
    print(f"Initial project: {destination / 'readonly'}")
    print(f"Python: {sys.executable}")
    print("Use the offline manual-test launch configuration in VS Code, then open the Mini Agent panel.")
    print("Select Mock in the panel. The repair scenario is a copy; the original example is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
