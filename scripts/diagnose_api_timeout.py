"""One authorized first-turn replay; record transport metadata, never payload/key/body."""
from dataclasses import replace
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import argparse
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from mini_agent.acceptance import suite, unchanged
from mini_agent.adapters.http_api import HTTPResult, ModelError, load_api_key
from mini_agent.config import load_config
from mini_agent.costs import CostBudget
from mini_agent.cli import build_parser
from mini_agent.controller import AgentController
from mini_agent.panel import PanelInteraction
from mini_agent.runtime import run_task


class TracedTransport:
    def __init__(self, record):
        self.record = record

    def post(self, endpoint, payload, key, deadline, limit, cancelled):
        start = time.monotonic()
        state = {"request_bytes": len(payload), "response_bytes": 0, "non_whitespace_bytes": 0}
        def phase(name, **data):
            state.update(phase=name, elapsed_seconds=round(time.monotonic() - start, 3), **data)
            self.record(**state)
        url = urlsplit(endpoint)
        connection = http.client.HTTPSConnection(url.hostname, url.port, timeout=max(.01, deadline - start))
        done = threading.Event()
        watcher = None
        try:
            phase("connect")
            connection.connect()
            phase("connected")
            sock = connection.sock
            def guard():
                while not done.wait(.02):
                    if cancelled() or time.monotonic() >= deadline:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
            watcher = threading.Thread(target=guard, daemon=True)
            watcher.start()
            phase("send_request")
            streaming = json.loads(payload).get("stream") is True
            connection.request("POST", url.path or "/", payload, {"Authorization": "Bearer " + key,
                "Content-Type": "application/json", "Accept": "text/event-stream" if streaming else "application/json", "Accept-Encoding": "identity"})
            phase("await_headers")
            response = connection.getresponse()
            phase("headers_received", http_status=response.status,
                content_type=response.getheader("Content-Type", "")[:100],
                content_length=response.getheader("Content-Length", "")[:20],
                transfer_encoding=response.getheader("Transfer-Encoding", "")[:30])
            if response.status != 200:
                return HTTPResult(response.status, b"")
            body = bytearray()
            phase("read_body")
            while chunk := response.read1(min(65536, limit + 1 - len(body))):
                body.extend(chunk)
                state["response_bytes"] = len(body)
                state["non_whitespace_bytes"] += len(chunk.strip())
                if streaming:
                    state["sse_events"] = sorted({name.decode("ascii") for name in re.findall(rb"(?m)^event: ([a-z._]+)", body)})
                phase("read_body")
                if len(body) > limit:
                    raise ModelError("MODEL_RESPONSE_TOO_LARGE", "Diagnostic response exceeds limit")
                if cancelled() or time.monotonic() >= deadline:
                    raise ModelError("MODEL_TIMEOUT", "Diagnostic deadline reached")
                if streaming and any(name in state.get("sse_events", []) for name in ("response.completed", "response.failed", "response.incomplete")):
                    break
            phase("complete")
            return HTTPResult(response.status, bytes(body))
        except (OSError, http.client.HTTPException) as exc:
            phase(state["phase"], error_type=type(exc).__name__)
            raise ModelError("MODEL_TIMEOUT" if isinstance(exc, TimeoutError) or time.monotonic() >= deadline else "MODEL_NETWORK_ERROR",
                             "See safe transport phase trace") from None
        finally:
            done.set()
            connection.close()
            if watcher:
                watcher.join(timeout=1)
            phase("closed", stopped_in=state["phase"])


def controls(root, *, protocols=False):
    config = load_config(root / "agent.api.example.json").api
    key = load_api_key(config)
    url = urlsplit(config.endpoint)
    destination = root / ".mini-agent/api-diagnostics" / ("controls-" + uuid.uuid4().hex[:12])
    destination.mkdir(parents=True)
    report = {"model": config.model, "probe_budget_limit_cny": 0.1, "repository_data_sent": False}
    started = time.monotonic()
    connection = http.client.HTTPSConnection(url.hostname, url.port, timeout=10)
    try:
        connection.request("GET", "/models", headers={"Authorization": "Bearer " + key, "Accept-Encoding": "identity"})
        response = connection.getresponse()
        report["authenticated_models"] = {"status": response.status}
        if response.status == 200:
            raw = response.read(128 * 1024 + 1)
            if len(raw) <= 128 * 1024:
                data = json.loads(raw)
                report["authenticated_models"]["configured_model_present"] = any(item.get("id") == config.model for item in data.get("data", []))
    except Exception as exc:
        report["authenticated_models"] = {"error_type": type(exc).__name__}
    finally:
        connection.close()
    report["authenticated_models"]["seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(report), flush=True)
    def probe(effort):
        cfg = replace(config, max_run_cost_cny=0.05, max_output_tokens=128, max_retries=0, reasoning_effort="none" if protocols else effort)
        budget = CostBudget(cfg)
        request = {"model": config.model, "stream": effort == "responses_stream", "store": False,
            "instructions": 'Return exactly this JSON object: {"ok":true}. No other text.',
            "input": "API availability check.", "reasoning": {"effort": "none" if protocols else effort},
            "text": {"format": {"type": "json_object"}}, "max_output_tokens": 128}
        endpoint = config.endpoint
        if effort == "chat_nonstream":
            endpoint = f"https://{url.netloc}/chat/completions"
            request = {"model": config.model, "stream": False, "reasoning_effort": "none", "max_tokens": 128,
                "messages": [{"role": "system", "content": request["instructions"]}, {"role": "user", "content": request["input"]}],
                "response_format": {"type": "json_object"}}
        payload = json.dumps(request).encode()
        reservation = budget.reserve(payload, 128)
        trace = []
        def record(**state):
            trace.append(state)
            print(json.dumps({"effort": effort, **state}), flush=True)
        result = {"effort": effort}
        try:
            response = TracedTransport(record).post(endpoint, payload, key, time.monotonic() + 30, 1024 * 1024, lambda: False)
            result["http_status"] = response.status
            if response.status == 200 and request["stream"]:
                result["sse_events"] = trace[-1].get("sse_events", [])
            elif response.status == 200:
                data = json.loads(response.body)
                result["response_status"] = data.get("status")
                usage = data.get("usage", {})
                if type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
                    budget.settle(reservation, usage["input_tokens"], usage["output_tokens"])
                result["json_response_received"] = True
        except Exception as exc:
            result["error_code"] = getattr(exc, "code", type(exc).__name__)
        result.update(trace=trace, cost=budget.summary())
        return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        report["probes"] = list(executor.map(probe, ("responses_stream", "chat_nonstream") if protocols else ("none", "low")))
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(destination / "report.json"), "results": [
        {k:v for k,v in p.items() if k not in {"trace", "cost"}} for p in report["probes"]]}), flush=True)


def native_control(root):
    config = load_config(root / "agent.api.example.json").api
    key = load_api_key(config)
    directory = root / ".mini-agent/api-diagnostics" / ("native-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    payload = json.dumps({"model": config.model, "stream": False,
        "instructions": 'Return exactly this JSON object: {"ok":true}. No other text.',
        "input": "API availability check.", "reasoning": {"effort": "none"},
        "text": {"format": {"type": "json_object"}}, "max_output_tokens": 128}).encode()
    cfg = replace(config, max_run_cost_cny=0.05)
    budget = CostBudget(cfg)
    budget.reserve(payload, 128)
    data_file = directory / "health-payload.json"
    data_file.write_bytes(payload)
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    # Secret headers travel through stdin, never argv, shell interpolation, or a file.
    curl_config = '\n'.join(["url = " + quote(config.endpoint),
        "header = " + quote("Authorization: Bearer " + key),
        'header = "Content-Type: application/json"', 'header = "Accept-Encoding: identity"',
        "data-binary = " + quote("@" + str(data_file))]) + '\n'
    process = subprocess.run(["curl.exe", "--config", "-", "--http1.1", "--noproxy", "*",
        "--silent", "--show-error", "--connect-timeout", "5", "--max-time", "20", "--output", "NUL",
        "--write-out", "%{json}"], input=curl_config, text=True, capture_output=True, timeout=25)
    metadata = json.loads(process.stdout)
    safe = {name: metadata.get(name) for name in ("http_code", "http_version", "time_connect", "time_appconnect",
        "time_starttransfer", "time_total", "size_upload", "size_download", "exitcode")}
    safe.update(client="native_curl_schannel", python_http_client_used=False, returncode=process.returncode,
                repository_data_sent=False, cost=budget.summary())
    (directory / "report.json").write_text(json.dumps(safe, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(directory / "report.json"), **safe}), flush=True)


def main():
    import io
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--controls", action="store_true")
    parser.add_argument("--protocol-controls", action="store_true")
    parser.add_argument("--native-control", action="store_true")
    args = parser.parse_args()
    if args.native_control:
        return native_control(root)
    if args.controls or args.protocol_controls:
        return controls(root, protocols=args.protocol_controls)
    frozen = root / ".pytest-tmp/v2-acceptance-20261015-02"
    manifest = suite(frozen)
    case = manifest["cases"][0]
    assert unchanged(frozen, case)
    evidence = root / ".mini-agent/api-diagnostics" / uuid.uuid4().hex[:12]
    trace = []
    class Controller(AgentController):
        def __init__(self, adapter, registry, audit, config, **kwargs):
            def record(**data):
                trace.append(data)
                audit.record("http_trace", **data)
                print(json.dumps(data), flush=True)
            adapter.transport = TracedTransport(record)
            super().__init__(adapter, registry, audit, replace(config, max_actions=1), **kwargs)
    class Interaction(PanelInteraction):
        def request(self, kind, payload):
            approved = kind == "disclosure" and set(payload["files"]) == set(case["initial_hashes"])
            self.audit.record("diagnostic_approval", kind=kind, approved=approved,
                              authorization="same_user_authorized_frozen_case_first_turn_replay")
            return approved
        def emit(self, kind, **data):
            pass
    args = build_parser().parse_args(["run", "--project", str(frozen / "projects" / case["id"]),
        "--task", case["task"], "--model", "api", "--allow-tests", "--config", str(root / "agent.api.example.json"),
        "--runs-dir", str(evidence)])
    interaction = Interaction(io.StringIO(), io.StringIO())
    result = run_task(args, interaction=interaction, controller_factory=Controller)
    print(json.dumps({"exit_code": result, "evidence": str(interaction.audit.directory)}), flush=True)


if __name__ == "__main__":
    main()
