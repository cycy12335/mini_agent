import subprocess
import sys

import pytest

from mini_agent.cli import main


def test_module_help():
    result = subprocess.run([sys.executable, "-m", "mini_agent", "--help"],
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    assert b"run" in result.stdout


def test_valid_run_reports_unimplemented_without_writing(tmp_path, capsys):
    source = tmp_path / "example.py"
    source.write_text("x = 1\n", encoding="utf-8")
    code = main(["run", "--project", str(tmp_path), "--task", "fix bug", "--read-only"])
    assert code == 1
    assert "failed" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.parametrize("extra", [["--task", " "], ["--allow-tests"],
                                 ["--read-only", "--allow-tests"]])
def test_invalid_arguments(tmp_path, extra):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--project", str(tmp_path), "--task", "fix", *extra])
    assert exc.value.code == 2


def test_missing_project(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--project", str(tmp_path / "missing"), "--task", "fix"])
    assert exc.value.code == 2
