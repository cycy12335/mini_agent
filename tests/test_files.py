from dataclasses import replace
import hashlib
import json

import pytest

from mini_agent.cli import main
from mini_agent.config import AgentConfig
from mini_agent.policy import PolicyGuard
from mini_agent.tools import FileTools


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "calculator.py").write_text(
        "def average(values):\n    return sum(values) / len(values)\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=average", encoding="utf-8")
    (root / "credentials.json").write_text('{"average": "secret"}', encoding="utf-8")
    (root / "image.png").write_bytes(b"average\0")
    (root / ".git").mkdir()
    (root / ".git" / "hidden.py").write_text("average", encoding="utf-8")
    return root


def make_tools(project, **budgets):
    return FileTools(PolicyGuard(project), replace(AgentConfig(), **budgets))


def test_list_read_search_share_policy_and_do_not_write(project):
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    tools = make_tools(project)
    listed = tools.list_files()
    assert listed.ok and not listed.truncated
    assert listed.data["files"] == ["src/calculator.py"]
    read = tools.read_file("src/calculator.py")
    assert read.ok and "def average" in read.data["content"]
    assert read.data["sha256"] == hashlib.sha256(before[next(p for p in before if p.name == "calculator.py")]).hexdigest()
    found = tools.search_text("average")
    assert found.ok and found.data["matches"] == [{
        "path": "src/calculator.py", "line": 1, "text": "def average(values):", "line_truncated": False}]
    for path in (".env", "credentials.json", ".git/hidden.py", "../outside.py"):
        assert not tools.read_file(path).ok
    after = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("content, code", [(b"x" * 65, "FILE_TOO_LARGE"),
    (b"abc\x00def", "NOT_TEXT"), (b"\xff\xfe", "NOT_TEXT")])
def test_invalid_text_is_not_returned_or_searched(project, content, code):
    (project / "bad.py").write_bytes(content)
    tools = make_tools(project, max_file_bytes=64)
    assert tools.read_file("bad.py").error_code == code
    assert tools.search_text("abc").data["matches"] == []


def test_file_byte_limit_and_utf8_bom(project):
    raw = b"\xef\xbb\xbf" + "你好\r\n".encode("utf-8")
    (project / "unicode.txt").write_bytes(raw)
    tools = make_tools(project, max_file_bytes=len(raw))
    result = tools.read_file("unicode.txt")
    assert result.ok and result.data["content"] == "你好\r\n"
    assert result.data["sha256"] == hashlib.sha256(raw).hexdigest()


def test_missing_and_directory_reads(project):
    tools = make_tools(project)
    assert tools.read_file("missing.py").error_code == "NOT_FOUND"
    assert tools.read_file("src").error_code == "NOT_FILE"
    assert tools.list_files("src/calculator.py").error_code == "NOT_DIRECTORY"


@pytest.mark.parametrize("query", ["", " ", None, 12, "a" * 257, "a\nb"])
def test_invalid_queries(project, query):
    assert make_tools(project).search_text(query).error_code == "INVALID_QUERY"


def test_search_is_literal_and_bounded(project):
    (project / "many.txt").write_text("a.*b\na.*b\na.*b\n", encoding="utf-8")
    tools = make_tools(project, max_search_hits=2)
    result = tools.search_text("a.*b")
    assert result.ok and result.truncated and len(result.data["matches"]) == 2
    assert tools.search_text("[unclosed").data["matches"] == []


def test_long_line_excerpt_keeps_the_match(project):
    (project / "long.txt").write_text("x" * 1000 + "needle" + "z" * 1000, encoding="utf-8")
    result = make_tools(project).search_text("needle")
    match = result.data["matches"][0]
    assert "needle" in match["text"] and len(match["text"]) == 500 and match["line_truncated"]


@pytest.mark.parametrize("budgets", [{"max_files": 1}, {"max_scan_entries": 1},
                                   {"max_tool_output_chars": 1}])
def test_listing_limits_report_truncation(project, budgets):
    (project / "extra.py").write_text("pass", encoding="utf-8")
    result = make_tools(project, **budgets).list_files()
    assert result.ok and result.truncated


def test_search_output_budget(project):
    result = make_tools(project, max_tool_output_chars=1).search_text("average")
    assert result.ok and result.truncated and not result.data["matches"]


def test_depth_budget(project):
    (project / "src" / "nested").mkdir()
    (project / "src" / "nested" / "code.py").write_text("pass", encoding="utf-8")
    result = make_tools(project, max_directory_depth=1).list_files()
    assert result.truncated and result.data["files"] == ["src/calculator.py"]


def test_cli_json_and_fail_closed(project, capsys):
    assert main(["files", "--project", str(project), "search", "average"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["data"]["matches"][0]["line"] == 1
    assert main(["files", "--project", str(project), "read", "../outside.py"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error_code"] == "INVALID_PATH" and result["data"] is None
