from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pytest

from mini_agent.actions import TOOL_SCHEMAS
from mini_agent.adapters.http_api import HTTPAdapter, HTTPResult, HTTPTransport, ModelError
from mini_agent.adapters.mock import MockAdapter
from mini_agent.api_config import APIConfig
from mini_agent.audit import AuditLog
from mini_agent.cli import main
from mini_agent.config import AgentConfig, ConfigError, load_config
from mini_agent.controller import AgentController
from mini_agent.privacy import Redactor, ScopedFiles, disclosure_files
from mini_agent.policy import PolicyGuard
from mini_agent.tools.files import FileTools
from mini_agent.tools.registry import ToolRegistry
from mini_agent.tools.tests import TestRunner


KEY = "test-credential-never-log-this"
FINAL = {"type": "final", "message": "No changes needed"}


def envelope(action=FINAL, protocol="responses", **extra):
    text = action if isinstance(action, str) else json.dumps(action)
    if protocol == "responses":
        data = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 12, "output_tokens": 3}}
    else:
        data = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
    return json.dumps({**data, **extra}).encode()


@contextmanager
def server(handler):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), json.loads(raw)))
            try:
                handler(self, len(requests))
            except (OSError, ValueError):
                pass  # Expected when the client cancels a deliberately slow response.
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1/responses", requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def reply(handler, status=200, body=None):
    body = envelope() if body is None else body
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def adapter(endpoint, **config):
    return HTTPAdapter(APIConfig(endpoint, "test-model", **config), key=KEY, authorized=True, timeout=2)


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_real_http_payload_and_response(protocol):
    with server(lambda h, n: reply(h, body=envelope(protocol=protocol))) as (endpoint, requests):
        model = adapter(endpoint, protocol=protocol)
        assert json.loads(model.next_action([{"role": "system", "task": "locate bug"}], TOOL_SCHEMAS)) == FINAL
    path, headers, payload = requests[0]
    assert path == "/v1/responses" and headers["Authorization"] == "Bearer " + KEY
    assert KEY not in json.dumps(payload)
    assert payload["store"] is False and payload["stream"] is False
    assert payload["model"] == "test-model"
    assert "JSON" in json.dumps(payload)
    assert model.usage_summary() == {"input_tokens": 12, "output_tokens": 3, "requests": 1, "unreported_requests": 0}


@pytest.mark.parametrize("status,attempts,code", [(401, 1, "MODEL_AUTH_ERROR"), (403, 1, "MODEL_AUTH_ERROR"),
    (400, 1, "MODEL_HTTP_ERROR"), (429, 3, "MODEL_RATE_LIMIT"), (503, 3, "MODEL_HTTP_ERROR"), (302, 1, "MODEL_HTTP_ERROR")])
def test_http_failures_have_bounded_retries_and_no_error_body_leak(status, attempts, code):
    with server(lambda h, n: reply(h, status, KEY.encode())) as (endpoint, requests):
        with pytest.raises(ModelError) as error:
            adapter(endpoint).next_action([], TOOL_SCHEMAS)
        assert error.value.code == code and KEY not in str(error.value)
        assert len(requests) == attempts


def test_temporary_failure_recovers_without_claiming_unknown_usage():
    with server(lambda h, n: reply(h, 503 if n == 1 else 200)) as (endpoint, requests):
        model = adapter(endpoint)
        model.next_action([], TOOL_SCHEMAS)
        assert len(requests) == 2 and model.usage_summary()["unreported_requests"] == 1


def test_redirect_does_not_forward_credentials():
    with server(lambda h, n: reply(h)) as (other, forwarded):
        def redirect(h, n):
            h.send_response(307)
            h.send_header("Location", other)
            h.end_headers()
        with server(redirect) as (endpoint, requests):
            with pytest.raises(ModelError):
                adapter(endpoint).next_action([], TOOL_SCHEMAS)
        assert not forwarded


@pytest.mark.parametrize("cancel", [False, True])
def test_slow_response_is_interrupted_with_total_deadline(cancel):
    def slow(h, n):
        h.send_response(200)
        h.send_header("Content-Length", "10000")
        h.end_headers()
        for _ in range(100):
            h.wfile.write(b" ")
            h.wfile.flush()
            time.sleep(.05)
    with server(slow) as (endpoint, requests):
        model = adapter(endpoint)
        start = time.monotonic()
        model.timeout = .3 if not cancel else 3
        model.cancelled = lambda: cancel and time.monotonic() - start > .2
        with pytest.raises(ModelError) as error:
            model.next_action([], TOOL_SCHEMAS)
        assert error.value.code == ("CANCELLED" if cancel else "MODEL_TIMEOUT")
        assert time.monotonic() - start < 2 and len(requests) == 1


def test_response_size_limit():
    with server(lambda h, n: reply(h, body=b"x" * 10000)) as (endpoint, requests):
        with pytest.raises(ModelError) as error:
            adapter(endpoint, max_response_bytes=100).next_action([], TOOL_SCHEMAS)
        assert error.value.code == "MODEL_RESPONSE_TOO_LARGE" and len(requests) == 1


@pytest.mark.parametrize("raw,code", [(b"not json", "MODEL_RESPONSE_FORMAT"), (b"[]", "MODEL_RESPONSE_FORMAT"),
    (envelope(status="incomplete"), "MODEL_INCOMPLETE"),
    (envelope(output=[]), "MODEL_RESPONSE_FORMAT"),
    (envelope(output=[{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}]), "MODEL_REFUSAL"),
    (envelope({"type": "final", "message": KEY}), "MODEL_SENSITIVE_OUTPUT")])
def test_bad_response_never_exposes_raw_payload(raw, code):
    with server(lambda h, n: reply(h, body=raw)) as (endpoint, requests):
        with pytest.raises(ModelError) as error:
            adapter(endpoint).next_action([], TOOL_SCHEMAS)
        assert error.value.code == code and KEY not in str(error.value)
        assert len(requests) == 1


def test_reasoning_is_discarded():
    model = adapter("https://example.com/responses")
    data = json.loads(envelope())
    data["output"].insert(0, {"type": "reasoning", "summary": "private reasoning " + KEY})
    assert json.loads(model._parse(json.dumps(data).encode())) == FINAL


def test_unauthorized_never_connects(monkeypatch):
    monkeypatch.setattr(HTTPTransport, "post", lambda *a: pytest.fail("Unauthorized HTTP call"))
    model = HTTPAdapter(APIConfig("https://example.com/responses", "test"), key=KEY)
    with pytest.raises(ModelError, match="Approve"):
        model.next_action([], TOOL_SCHEMAS)


@pytest.mark.parametrize("endpoint", ["http://example.com/api", "https://user:secret@example.com/api",
    "https://example.com/api?key=secret", "https://example.com/api#fragment", "file:///tmp/api", "https://example.com/\napi"])
def test_unsafe_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        APIConfig(endpoint, "test")


@pytest.mark.parametrize("api", [{"endpoint": "https://example.com", "model": "test", "api_key": KEY},
    {"endpoint": "https://example.com", "model": "test", "max_retries": 3},
    {"endpoint": "https://example.com", "model": "test", "api_key_env": "invalid\nheader"},
    {"endpoint": "https://example.com", "model": "test", "max_output_tokens": True}])
def test_api_config_is_strict(tmp_path, api):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": api}), encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert KEY not in str(error.value)


def test_frozen_scope_and_sensitive_files(project):
    policy, config = PolicyGuard(project), AgentConfig()
    files = FileTools(policy, config)
    allowed = disclosure_files(files, ["src"])
    scoped = ScopedFiles(policy, config, allowed, Redactor([KEY]))
    assert scoped.read_file("tests/test_calculator.py").error_code == "SEND_SCOPE_DENIED"
    (project / "src/new.py").write_text("value = 1\n", encoding="utf-8")
    assert scoped.read_file("src/new.py").error_code == "SEND_SCOPE_DENIED"
    assert scoped.list_files().data["files"] == ["src/calculator.py"]
    (project / "src/calculator.py").write_text(f'api_key = "{KEY}"\n', encoding="utf-8")
    assert scoped.read_file("src/calculator.py").error_code == "SENSITIVE_CONTENT"
    assert not scoped.search_text(KEY).data["matches"]


def test_audit_redaction_preserves_valid_json(project, tmp_path):
    audit = AuditLog(tmp_path / "runs", project, redactor=Redactor([KEY]))
    audit.record("event", nested={"output": "api_key=" + KEY})
    audit.write_json("summary.json", {"nested": {"message": KEY, "other": "password=abc"}, "ok": True})
    audit.write_text("test-output.txt", KEY)
    for path in audit.directory.iterdir():
        assert KEY not in path.read_text(encoding="utf-8")
    assert audit.read_json("summary.json")["ok"] is True
    assert json.loads(audit.events.read_text(encoding="utf-8"))["nested"]["output"] == "api_key=[REDACTED]"


def api_cli_args(project, tmp_path, endpoint):
    path = tmp_path / "api.json"
    path.write_text(json.dumps({"api": {"endpoint": endpoint, "model": "test-model"}}), encoding="utf-8")
    return ["run", "--project", str(project), "--task", "average([]) should return zero", "--model", "api",
            "--config", str(path), "--runs-dir", str(tmp_path / "runs")]


def test_cli_declined_disclosure_makes_no_requests(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_AGENT_API_KEY", KEY)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with server(lambda h, n: reply(h)) as (endpoint, requests):
        assert main([*api_cli_args(project, tmp_path, endpoint), "--read-only", "--send-path", "src"]) == 3
        assert not requests
    out = capsys.readouterr().out
    assert endpoint in out and "src/calculator.py" in out and KEY not in out
    summary = json.loads(next((tmp_path / "runs").glob("*/summary.json")).read_text(encoding="utf-8"))
    assert summary["status"] == "cancelled" and summary["tests"] == "not_run"


def test_real_http_cli_repair_is_offline_protocol_integration(project, tmp_path, interpreter, monkeypatch):
    # This local scripted server validates transport/controller integration, NOT real-model acceptance.
    mock = MockAdapter()
    def respond(h, n):
        data = json.loads(requests[-1][2]["input"])
        action = mock.next_action(data, TOOL_SCHEMAS)
        reply(h, body=envelope(action))
    with server(respond) as (endpoint, requests):
        monkeypatch.setenv("MINI_AGENT_API_KEY", KEY)
        answers = iter(["y", "y"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        assert main([*api_cli_args(project, tmp_path, endpoint), "--allow-tests", "--test-python", str(interpreter)]) == 0
    summary_path = next((tmp_path / "runs").glob("*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "verified" and len(requests) == 6
    assert summary["token_usage"]["input_tokens"] == 72
    for path in summary_path.parent.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


def test_missing_credential_never_creates_run_or_connects(project, tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)
    monkeypatch.setattr(HTTPTransport, "post", lambda *a: pytest.fail("Missing credential must not connect"))
    with pytest.raises(SystemExit) as error:
        main(api_cli_args(project, tmp_path, "https://example.com/responses"))
    assert error.value.code == 2 and not (tmp_path / "runs").exists()


def test_run_budget_and_precancellation_prevent_connections(monkeypatch):
    monkeypatch.setattr(HTTPTransport, "post", lambda *a: pytest.fail("Budget/cancel must prevent connection"))
    model = adapter("https://example.com/responses")
    model.set_run_budget(-1, lambda: False)
    with pytest.raises(ModelError) as error:
        model.next_action([], TOOL_SCHEMAS)
    assert error.value.code == "MODEL_TIMEOUT"
    model.set_run_budget(60, lambda: True)
    with pytest.raises(ModelError) as error:
        model.next_action([], TOOL_SCHEMAS)
    assert error.value.code == "CANCELLED"


def test_network_retries_are_capped():
    class BrokenTransport:
        calls = 0
        def post(self, *args):
            self.calls += 1
            raise ModelError("MODEL_NETWORK_ERROR", "Connection failed")
    transport = BrokenTransport()
    model = HTTPAdapter(APIConfig("https://example.com/responses", "test"), key=KEY,
                        transport=transport, authorized=True)
    with pytest.raises(ModelError):
        model.next_action([], TOOL_SCHEMAS)
    assert transport.calls == 3 and model.usage_summary() == "unknown"


def test_outbound_redaction_and_invalid_action_correction(project, tmp_path, monkeypatch):
    actions = ["malformed " + KEY, {"type": "tool_call", "name": "read_file", "arguments": {"path": "src/calculator.py"}}, FINAL]
    with server(lambda h, n: reply(h, body=envelope(actions[n - 1]))) as (endpoint, requests):
        monkeypatch.setenv("MINI_AGENT_API_KEY", KEY)
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        args = api_cli_args(project, tmp_path, endpoint)
        args[args.index("--task") + 1] = "Please fix this bug. Credential: " + KEY
        assert main([*args, "--read-only"]) == 0
        assert len(requests) == 3
        assert all(KEY not in json.dumps(request[2]) for request in requests)
    for path in (tmp_path / "runs").rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8")


def test_second_invalid_action_stops_with_no_write(project, tmp_path, monkeypatch):
    source = (project / "src/calculator.py").read_bytes()
    with server(lambda h, n: reply(h, body=envelope({"type": "tool_call", "name": "shell", "arguments": {"command": "bad"}}))) as (endpoint, requests):
        monkeypatch.setenv("MINI_AGENT_API_KEY", KEY)
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert main(api_cli_args(project, tmp_path, endpoint)) == 1
        assert len(requests) == 2
    assert (project / "src/calculator.py").read_bytes() == source


def test_scope_inventory_must_not_be_truncated(project):
    with pytest.raises(ValueError, match="truncated"):
        disclosure_files(FileTools(PolicyGuard(project), AgentConfig(max_files=1)), ["."])


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_truncation_and_refusal_are_not_valid_actions(protocol):
    model = adapter("https://example.com/api", protocol=protocol)
    data = json.loads(envelope(protocol=protocol))
    if protocol == "responses":
        data["status"] = "incomplete"
    else:
        data["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ModelError) as error:
        model._parse(json.dumps(data).encode())
    assert error.value.code == "MODEL_INCOMPLETE"


def test_deepseek_example_matches_official_responses_contract():
    config = load_config(Path(__file__).resolve().parents[1] / "agent.api.example.json")
    assert config.api.endpoint == "https://api.deepseek.com/responses"
    assert config.api.model == "deepseek-flash" and config.api.protocol == "responses"
    model = HTTPAdapter(config.api, key=KEY)
    payload = model._payload([], TOOL_SCHEMAS)
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"]["format"] == {"type": "json_object"}
    assert "max_tokens" not in payload and payload["max_output_tokens"] == 8192
