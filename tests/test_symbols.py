import hashlib
import json
from pathlib import Path
import time

import pytest

from mini_agent.actions import ActionError, TOOL_SCHEMAS, validate_action
from mini_agent.adapters.mock import call
from mini_agent.cli import main
from mini_agent.config import AgentConfig
from mini_agent.policy import PolicyGuard
from mini_agent.privacy import Redactor, ScopedFiles
from mini_agent.symbols import SymbolIndex
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def index(root, config=None):
    return SymbolIndex(FileTools(PolicyGuard(root), config))


def query(symbols, operation, symbol, path=None):
    result = symbols.query("find_" + operation, symbol, path)
    assert result.ok, result.to_dict()
    return result.data["matches"]


@pytest.fixture
def cross_file(tmp_path):
    put(tmp_path, "src/shop/__init__.py", "from .maths import total\n")
    put(tmp_path, "src/shop/maths.py", "def total(items):\n    return sum(items)\n")
    put(tmp_path, "src/shop/service.py", "from . import total as calculate\n\ndef checkout(items):\n    return calculate(items)\n")
    put(tmp_path, "tests/test_service.py", "from shop.service import checkout as run\n\ndef test_checkout():\n    assert run([1, 2]) == 3\n")
    return tmp_path


def test_cross_file_definitions_aliases_call_graph_and_related_tests(cross_file):
    symbols = index(cross_file)
    definitions = query(symbols, "definition", "total")
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["symbol"] == "shop.maths.total"
    assert definition["line"] == 1 and definition["end_line"] == 2
    assert definition["sha256"] == hashlib.sha256((cross_file / definition["path"]).read_bytes()).hexdigest()
    assert query(symbols, "definition", "shop.total")[0]["id"] == definition["id"]
    callers = query(symbols, "callers", definition["id"])
    assert [(item["path"], item["line"], item["expression"]) for item in callers] == [("src/shop/service.py", 4, "calculate")]
    assert not callers[0]["uncertain"]
    references = query(symbols, "references", "total")
    assert {item["kind"] for item in references} == {"call", "import"}
    callees = query(symbols, "callees", "checkout")
    assert callees[0]["targets"] == [definition["id"]]
    related = query(symbols, "related_tests", "total")
    assert [(item["name"], item["distance"]) for item in related] == [("test_checkout", 2)]


def test_namespace_src_module_imports_match_example(project):
    matches = query(index(project), "callers", "average")
    assert len(matches) == 2
    assert {item["line"] for item in matches} == {5, 9}


def test_module_import_and_reexport_alias(cross_file):
    put(cross_file, "entry.py", "import shop.maths as maths\nimport shop.service\n\ndef run():\n    maths.total([])\n    shop.service.checkout([])\n")
    symbols = index(cross_file)
    assert any(item["path"] == "entry.py" for item in query(symbols, "callers", "total"))
    assert any(item["expression"] == "shop.service.checkout" for item in query(symbols, "callers", "checkout"))


def test_ambiguous_names_require_path_or_id(tmp_path):
    put(tmp_path, "first.py", "def run(): pass\n")
    put(tmp_path, "second.py", "def run(): pass\n")
    symbols = index(tmp_path)
    result = symbols.query("find_definition", "run")
    assert result.data["ambiguous"] and len(result.data["matches"]) == 2
    assert len(query(symbols, "definition", "run", "first.py")) == 1
    identity = result.data["matches"][1]["id"]
    assert query(symbols, "definition", identity)[0]["path"] == "second.py"


def test_shadowing_closures_globals_and_class_lookup(tmp_path):
    put(tmp_path, "example.py", '''def target(): pass
def parameter(target):
    target()
def local():
    target()
    target = lambda: None
def outer():
    def inner():
        target()
    return inner
class Example:
    target = 1
    def method(self):
        target()
def explicit_global():
    global target
    target()
''')
    symbols = index(tmp_path)
    callers = query(symbols, "callers", "target", "example.py")
    assert {item["line"] for item in callers} == {9, 14, 17}
    assert all(not item["uncertain"] for item in callers)
    assert query(symbols, "callees", "parameter")[0]["reason"] == "parameter"
    assert query(symbols, "callees", "local")[0]["reason"] == "assignment_or_deletion"


def test_static_methods_and_dynamic_receiver_candidates(tmp_path):
    put(tmp_path, "model.py", '''class Worker:
    def execute(self): pass
    async def run(self):
        self.execute()
def dispatch(worker):
    worker.execute()
def static():
    Worker.execute(None)
''')
    symbols = index(tmp_path)
    callers = query(symbols, "callers", "Worker.execute")
    assert len(callers) == 2
    assert callers[0]["uncertain"] and callers[0]["reason"] == "dynamic_receiver"
    assert not callers[1]["uncertain"]
    dispatch = query(symbols, "callees", "dispatch")[0]
    assert dispatch["uncertain"] and not dispatch["targets"]
    assert query(symbols, "definition", "run")[0]["kind"] == "async_function"


def test_runtime_imports_decorators_wildcards_and_reflection(tmp_path):
    put(tmp_path, "library.py", "def target(): pass\n")
    put(tmp_path, "entry.py", '''def run(obj):
    from library import target
    target()
    getattr(obj, "target")()
''')
    put(tmp_path, "decorated.py", "@unknown\ndef target(): pass\ndef run(): target()\n")
    put(tmp_path, "wildcard.py", "from library import *\ndef run(): target()\n")
    symbols = index(tmp_path)
    runtime = query(symbols, "callers", "library.target")
    assert len(runtime) == 1 and runtime[0]["reason"] == "runtime_import" and runtime[0]["uncertain"]
    assert query(symbols, "callers", "decorated.target")[0]["reason"] == "decorated_binding"
    assert any(item["reason"] == "dynamic_expression" for item in query(symbols, "callees", "run", "entry.py"))
    assert query(symbols, "callees", "run", "wildcard.py")[0]["reason"] == "wildcard_import"
    assert any(item["target"] == "library" and item["uncertain"] for item in symbols.imports)


def test_lambda_and_comprehension_scopes_do_not_shadow_enclosing_calls(tmp_path):
    put(tmp_path, "entry.py", '''def target(): pass
def run():
    target()
    callback = lambda target: target()
    [target() for target in []]
    target()
''')
    callers = query(index(tmp_path), "callers", "target")
    assert {item["line"] for item in callers} == {3, 6}


def test_incremental_update_handles_edit_delete_and_new_files(cross_file):
    symbols = index(cross_file)
    query(symbols, "definition", "total")
    parsed = symbols.parsed_files
    query(symbols, "definition", "total")
    assert symbols.parsed_files == parsed
    put(cross_file, "src/shop/maths.py", "def different(items): return items\n")
    assert not query(symbols, "definition", "total")
    assert symbols.parsed_files == parsed + 1
    (cross_file / "src/shop/maths.py").unlink()
    put(cross_file, "new.py", "def fresh(): pass\n")
    assert query(symbols, "definition", "fresh")
    assert "src/shop/maths.py" not in symbols.cache


def test_newly_sensitive_and_invalid_files_evict_cached_symbols(tmp_path):
    path = put(tmp_path, "entry.py", "def safe(): pass\n")
    symbols = index(tmp_path)
    assert query(symbols, "definition", "safe")
    path.write_text("def safe(:\n", encoding="utf-8")
    result = symbols.query("find_definition", "safe")
    assert not result.data["matches"]
    assert result.data["coverage"]["errors"] == [{"path": "entry.py", "line": 1, "code": "SYNTAX_ERROR"}]
    path.write_text("secret = 'NEVER_DISCLOSE'\ndef safe(): pass\n", encoding="utf-8")
    result = symbols.query("find_definition", "safe")
    assert not result.data["matches"] and "NEVER_DISCLOSE" not in json.dumps(result.to_dict())
    assert result.data["coverage"]["errors"][0]["code"] == "SENSITIVE_CONTENT"


def test_send_scope_and_filtered_paths_apply_to_all_queries(cross_file):
    files = ScopedFiles(PolicyGuard(cross_file), AgentConfig(), ["src/shop/service.py"], Redactor())
    symbols = SymbolIndex(files)
    result = symbols.query("find_definition", "total")
    assert not result.data["matches"] and result.data["coverage"]["indexed_files"] == 1
    assert not files.read_paths  # scans alone do not claim returned source evidence
    assert symbols.query("find_definition", "total", "src/shop/maths.py").error_code == "SEND_SCOPE_DENIED"
    assert symbols.query("find_definition", "total", "../other.py").error_code == "INVALID_PATH"
    put(cross_file, "secrets.py", "def hidden(): pass")
    assert not query(symbols, "definition", "hidden")


@pytest.mark.parametrize("config", [AgentConfig(max_index_bytes=10), AgentConfig(max_index_nodes=5), AgentConfig(max_files=1)])
def test_index_limits_are_explicit(cross_file, config):
    result = index(cross_file, config).query("find_definition", "total")
    assert result.ok and result.truncated and result.data["coverage"]["limited"]


def test_query_output_and_hits_are_bounded(tmp_path):
    put(tmp_path, "entry.py", "def target(): pass\ndef run():\n" + "    target()\n" * 50)
    config = AgentConfig(max_search_hits=2, max_tool_output_chars=1600)
    result = index(tmp_path, config).query("find_callers", "target")
    assert result.ok and result.truncated
    assert len(result.data["matches"]) <= 2
    assert len(json.dumps(result.data, ensure_ascii=False)) <= 1600


def test_cancel_and_time_budget_stop_before_reading(tmp_path):
    symbols = index(tmp_path)
    symbols.cancelled = lambda: True
    assert symbols.query("find_definition", "target").error_code == "CANCELLED"
    symbols.cancelled = lambda: False
    symbols.deadline = time.monotonic() - 1
    assert symbols.query("find_definition", "target").error_code == "TIME_LIMIT"


@pytest.mark.parametrize("operation", ["definition", "references", "callers", "callees", "related_tests"])
def test_tool_protocol_and_registry_dispatch(cross_file, operation):
    action = call("find_" + operation, symbol="total")
    assert validate_action(action, AgentConfig()) == action
    assert any(schema["name"] == action["name"] for schema in TOOL_SCHEMAS)
    registry = ToolRegistry(FileTools(PolicyGuard(cross_file)))
    assert registry.execute(action).ok
    with pytest.raises(ActionError):
        registry.execute(call(action["name"], symbol="total", command="run something"))
    with pytest.raises(ActionError):
        registry.execute(call(action["name"], symbol="\n"))


def test_offline_cli(cross_file, capsys):
    assert main(["symbols", "--project", str(cross_file), "callers", "total"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["data"]["matches"][0]["path"] == "src/shop/service.py"


def test_custom_source_root_changes_invalidate_module_names(tmp_path):
    put(tmp_path, "lib/widget/service.py", "def target(): pass")
    config = put(tmp_path, "pyproject.toml", '[tool.setuptools.package-dir]\n""="lib"\n')
    symbols = index(tmp_path)
    assert query(symbols, "definition", "widget.service.target")
    config.write_text('[tool.setuptools.package-dir]\n"custom"="lib/widget"\n', encoding="utf-8")
    assert query(symbols, "definition", "custom.service.target")
    assert not query(symbols, "definition", "widget.service.target")


def test_rebound_module_export_is_unresolved(tmp_path):
    put(tmp_path, "library.py", "def target(): pass\ntarget = unknown\n")
    put(tmp_path, "entry.py", "from library import target\ndef run(): target()\n")
    symbols = index(tmp_path)
    assert not query(symbols, "callers", "library.target")
    assert query(symbols, "callees", "run")[0]["reason"] == "ambiguous_or_rebound_binding"


def test_nonlocal_writes_and_comprehension_walrus_are_conservative(tmp_path):
    put(tmp_path, "entry.py", '''def target(): pass
def outer():
    def inner(): pass
    def change():
        nonlocal inner
        inner = unknown
    inner()
def run():
    [(target := value) for value in []]
    target()
''')
    symbols = index(tmp_path)
    assert not query(symbols, "callers", "inner")
    assert not query(symbols, "callers", "target")


def test_conditional_import_and_deferred_calls_are_uncertain(tmp_path):
    put(tmp_path, "library.py", "def target(): pass\n")
    put(tmp_path, "entry.py", '''if unknown:
    from library import target
def run():
    callback = lambda: target()
    iterator = (target() for value in [])
    target()
''')
    symbols = index(tmp_path)
    callers = query(symbols, "callers", "library.target")
    assert len(callers) == 3 and all(item["uncertain"] for item in callers)
    assert [item["reason"] for item in callers] == ["deferred_expression_body", "deferred_expression_body", "runtime_import"]


def test_related_tests_depth_limit_is_visible(tmp_path):
    code = "def target(): pass\n"
    for depth in range(10):
        prior = "target" if depth == 0 else f"step_{depth - 1}"
        code += f"def step_{depth}(): {prior}()\n"
    code += "def test_deep(): step_9()\n"
    put(tmp_path, "tests/test_deep.py", code)
    result = index(tmp_path).query("find_related_tests", "target")
    assert result.ok and result.truncated
    assert result.data["coverage"]["related_tests_depth_limited"]


def test_controller_traces_cross_file_evidence_and_records_queries(cross_file):
    from mini_agent.audit import AuditLog
    from mini_agent.controller import AgentController
    actions = iter([call("find_definition", symbol="checkout"),
                    call("find_callees", symbol="checkout"),
                    call("find_definition", symbol="total"),
                    call("find_related_tests", symbol="total"),
                    {"type": "final", "message": "checkout calls shop.maths.total; test_checkout is a static related test. Runtime behavior unverified."}])
    class Adapter:
        def next_action(self, messages, schemas):
            assert any(schema["name"] == "find_callees" for schema in schemas)
            return next(actions)
    audit = AuditLog(cross_file.parent / (cross_file.name + "-runs"), cross_file)
    before = {str(path): path.read_bytes() for path in cross_file.rglob("*") if path.is_file()}
    registry = ToolRegistry(FileTools(PolicyGuard(cross_file)))
    result = AgentController(Adapter(), registry, audit, AgentConfig(), read_only=True).run("Trace checkout bug")
    assert result.status == "proposed" and result.actions == 5
    events = [json.loads(line) for line in audit.events.read_text(encoding="utf-8").splitlines()]
    results = {event["name"]: event["result"] for event in events if event["event"] == "tool_result"}
    assert results["find_callees"]["data"]["matches"][0]["targets"] == ["src/shop/maths.py::total@1"]
    assert results["find_related_tests"]["data"]["matches"][0]["path"] == "tests/test_service.py"
    assert before == {str(path): path.read_bytes() for path in cross_file.rglob("*") if path.is_file()}


def test_disclosure_decline_never_builds_index(cross_file, monkeypatch):
    from mini_agent.audit import AuditLog
    from mini_agent.controller import AgentController
    registry = ToolRegistry(FileTools(PolicyGuard(cross_file)))
    monkeypatch.setattr(registry.symbols, "refresh", lambda: pytest.fail("Indexing before disclosure"))
    class Adapter:
        def next_action(self, *args):
            pytest.fail("Model called after decline")
    audit = AuditLog(cross_file.parent / (cross_file.name + "-runs"), cross_file)
    result = AgentController(Adapter(), registry, audit, AgentConfig(), before_run=lambda: False).run("bug")
    assert result.status == "cancelled" and not registry.symbols.cache
