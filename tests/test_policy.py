import os
from pathlib import Path
import subprocess

import pytest

from mini_agent.models import ToolError
from mini_agent.policy import PolicyGuard


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "code.py").write_text("value = 1\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("path", ["../code.py", "a/../code.py", "/etc/passwd",
    "C:\\secret.py", "C:secret.py", "\\\\server\\share\\code.py", "\\root.py",
    "a//code.py", "a/./code.py", "code.py:stream", "NUL.py", "COM1", "code.py ",
    "code.py.", "a\x00.py", "", None, 42, "a" * 1025])
def test_unsafe_paths_rejected(project, path):
    with pytest.raises(ToolError) as exc:
        PolicyGuard(project).resolve(path)
    assert exc.value.code == "INVALID_PATH"


@pytest.mark.parametrize("path", [".env", ".env.example", ".git/config", ".venv/code.py",
    "node_modules/pkg/code.py", "credentials.json", "secrets.yaml", "api_key.txt",
    "service-account.json", "id_rsa", ".aws/config", "passwords.txt"])
def test_sensitive_paths_rejected_before_access(project, path):
    with pytest.raises(ToolError) as exc:
        PolicyGuard(project).resolve(path)
    assert exc.value.code == "FILTERED_PATH"


def test_real_file_and_backslash_relative_path(project):
    (project / "src").mkdir()
    (project / "src" / "code.py").write_text("pass", encoding="utf-8")
    guard = PolicyGuard(project)
    assert guard.resolve("src\\code.py") == project / "src" / "code.py"
    assert guard.resolve(".", directory=True) == project


def test_agent_project_is_protected():
    with pytest.raises(ToolError, match="separate"):
        PolicyGuard(Path(__file__).resolve().parents[1])


def test_source_checkout_is_protected_independently_of_installed_package(tmp_path):
    checkout = tmp_path / "checkout"
    package = checkout / "src/mini_agent"
    package.mkdir(parents=True)
    for name in ("__init__.py", "cli.py", "policy.py"):
        (package / name).write_text("# fixture\n", encoding="utf-8")
    child = package / "tools"
    child.mkdir()
    for target in (checkout, checkout / "src", package, child):
        with pytest.raises(ToolError, match="separate"):
            PolicyGuard(target)
    example = checkout / "examples/target"
    example.mkdir(parents=True)
    assert PolicyGuard(example).root == example.resolve()


def test_symlink_file_and_root_rejected(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("secret", encoding="utf-8")
    link = project / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    with pytest.raises(ToolError, match="links"):
        PolicyGuard(project).resolve("link/outside.py")
    with pytest.raises(ToolError, match="links"):
        PolicyGuard(link)


def test_hardlinked_file_rejected(project, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    os.link(outside, project / "alias.py")
    with pytest.raises(ToolError, match="Hard-linked"):
        PolicyGuard(project).resolve("alias.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_junction_rejected(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("secret", encoding="utf-8")
    junction = project / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                   capture_output=True, check=True, timeout=10)
    try:
        with pytest.raises(ToolError, match="reparse"):
            PolicyGuard(project).resolve("junction/outside.py")
        with pytest.raises(ToolError, match="reparse"):
            PolicyGuard(junction)
    finally:
        # Remove just the directory entry, never traverse or delete its target.
        os.rmdir(junction)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO test")
def test_fifo_is_not_readable(project):
    os.mkfifo(project / "pipe.py")
    with pytest.raises(ToolError, match="regular"):
        PolicyGuard(project).resolve("pipe.py")
