"""Bounded, non-streaming JSON action adapter. No raw response/credential logging."""

from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from ..api_config import APIConfig
from ..models import ToolError
from ..policy import _check_links
from ..privacy import Redactor
from ..prompts import INSTRUCTIONS
from ..costs import CostBudget


class ModelError(ToolError):
    pass


@dataclass
class HTTPResult:
    status: int
    body: bytes


class HTTPTransport:
    """Direct TLS; no inherited proxy, redirect, cookie or authentication handlers."""

    def post(self, endpoint, payload, key, deadline, limit, cancelled):
        url = urlsplit(endpoint)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError("MODEL_TIMEOUT", "Model request budget exhausted")
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = cls(url.hostname, url.port, timeout=remaining)
        done = threading.Event()
        watchdog = None
        try:
            connection.connect()
            # DNS resolution is OS-managed. Check again before transmitting any payload.
            if cancelled():
                raise ModelError("CANCELLED", "Model request cancelled")
            if time.monotonic() >= deadline:
                raise ModelError("MODEL_TIMEOUT", "Model request budget exhausted during connection")
            sock = connection.sock
            def guard():
                while not done.wait(0.02):
                    if cancelled() or time.monotonic() >= deadline:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
            watchdog = threading.Thread(target=guard, daemon=True)
            watchdog.start()
            connection.request("POST", url.path or "/", payload,
                               {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                "Accept": "application/json", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status != 200:
                return HTTPResult(response.status, b"")  # Error bodies may echo credentials.
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ModelError("MODEL_RESPONSE_FORMAT", "Compressed responses are unsupported")
            body = bytearray()
            while chunk := response.read1(min(65536, limit + 1 - len(body))):
                body.extend(chunk)
                if len(body) > limit:
                    raise ModelError("MODEL_RESPONSE_TOO_LARGE", "Model response exceeds byte budget")
                if cancelled():
                    raise ModelError("CANCELLED", "Model request cancelled")
                if time.monotonic() >= deadline:
                    raise ModelError("MODEL_TIMEOUT", "Model request budget exhausted")
            return HTTPResult(response.status, bytes(body))
        except ssl.SSLCertVerificationError as exc:
            raise ModelError("MODEL_TLS_ERROR", "TLS certificate verification failed") from exc
        except (OSError, http.client.HTTPException) as exc:
            if cancelled():
                raise ModelError("CANCELLED", "Model request cancelled") from exc
            if isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
                raise ModelError("MODEL_TIMEOUT", "Model request budget exhausted") from exc
            raise ModelError("MODEL_NETWORK_ERROR", "Model connection failed") from exc
        finally:
            done.set()
            connection.close()
            if watchdog:
                watchdog.join(timeout=1)


CREDENTIALS_FILE = Path("agent.credentials.json")


def _credential_object(pairs):
    data = {}
    for name, value in pairs:
        if name in data:
            raise ValueError("Duplicate credential field")
        data[name] = value
    return data


def load_api_key(config):
    # Read on every run; never include credential values in errors or configuration.
    key = ""
    credentials = CREDENTIALS_FILE.absolute()
    if credentials.exists() or credentials.is_symlink():
        try:
            _check_links(credentials)
            if credentials.stat().st_nlink > 1:
                raise ValueError("Linked credential file")
            with credentials.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("Credential file too large")
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_credential_object)
            if not isinstance(data, dict) or set(data) != {"api_key"} or not isinstance(data["api_key"], str):
                raise ValueError("Invalid credential fields")
            key = data["api_key"]
        except (OSError, ValueError, RuntimeError, RecursionError):
            raise ModelError("MODEL_CREDENTIAL_INVALID", "Cannot read agent.credentials.json; expected UTF-8 JSON with only a string api_key field") from None
    if not key:
        key = os.environ.get(config.api_key_env, "") if config.api_key_env else ""
    if not key or key == config.api_key_env or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ModelError("MODEL_CREDENTIAL_MISSING", "Set the actual secret value in agent.credentials.json or the configured environment variable; the variable name is not a credential")
    return key


class HTTPAdapter:
    def __init__(self, config: APIConfig, *, key=None, timeout=60, transport=None,
                 redactor=None, authorized=False):
        self.config = config
        self._key = load_api_key(config) if key is None else key
        if not isinstance(self._key, str) or not self._key or self._key == config.api_key_env or len(self._key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self._key):
            raise ModelError("MODEL_CREDENTIAL_MISSING", "Invalid API credential")
        self.timeout = timeout
        self.transport = transport or HTTPTransport()
        self.redactor = redactor or Redactor([self._key])
        self.authorized = authorized
        self.cancelled = lambda: False
        self.run_deadline = None
        self.requests = 0
        self.usage_records = []
        self.reported_models = set()
        self.cost_budget = CostBudget(config)
        self._pending_reservation = None
        self.cost_observer = lambda **data: None

    def set_run_budget(self, remaining, cancelled):
        self.run_deadline = time.monotonic() + remaining
        self.cancelled = cancelled

    def _payload(self, messages, schemas):
        # Internal tool messages are serialized task data, not provider-native tool grants.
        instructions = INSTRUCTIONS + "\nTool schemas: " + json.dumps(schemas, ensure_ascii=False)
        context = json.dumps(self.redactor.value(messages), ensure_ascii=False)
        common = {"model": self.config.model, "store": False, "stream": False}
        if self.config.protocol == "responses":
            return {**common, "instructions": instructions, "input": context,
                    **({"reasoning": {"effort": self.config.reasoning_effort}} if self.config.reasoning_effort else {}),
                    "text": {"format": {"type": "json_object"}}, "max_output_tokens": self.config.max_output_tokens}
        return {**common, "messages": [{"role": "system", "content": instructions},
                                        {"role": "user", "content": context}],
                **({"reasoning_effort": self.config.reasoning_effort} if self.config.reasoning_effort else {}),
                "response_format": {"type": "json_object"}, "max_completion_tokens": self.config.max_output_tokens}

    def next_action(self, messages, tool_schemas):
        if not self.authorized:
            raise ModelError("MODEL_NOT_AUTHORIZED", "Approve the endpoint and disclosure manifest before calling the model")
        deadline = time.monotonic() + self.timeout
        if self.run_deadline is not None:
            deadline = min(deadline, self.run_deadline)
        payload = json.dumps(self._payload(messages, tool_schemas), ensure_ascii=False).encode("utf-8")
        for attempt in range(self.config.max_retries + 1):
            if self.cancelled():
                raise ModelError("CANCELLED", "Model request cancelled")
            if time.monotonic() >= deadline:
                raise ModelError("MODEL_TIMEOUT", "Model request budget exhausted")
            self._pending_reservation = self.cost_budget.reserve(payload, self.config.max_output_tokens)
            self.requests += 1
            self.cost_observer(phase="reserved", request=self.requests, budget=self.cost_summary())
            try:
                result = self.transport.post(self.config.endpoint, payload, self._key, deadline,
                                             self.config.max_response_bytes, self.cancelled)
            except ModelError as exc:
                if exc.code != "MODEL_NETWORK_ERROR" or attempt == self.config.max_retries:
                    raise
            else:
                if self.cancelled():
                    raise ModelError("CANCELLED", "Model request cancelled")
                if time.monotonic() >= deadline:
                    raise ModelError("MODEL_TIMEOUT", "Model request budget exhausted")
                if result.status == 200:
                    if len(result.body) > self.config.max_response_bytes:
                        raise ModelError("MODEL_RESPONSE_TOO_LARGE", "Model response exceeds byte budget")
                    return self._parse(result.body)
                code = ("MODEL_AUTH_ERROR" if result.status in {401, 403} else
                        "MODEL_RATE_LIMIT" if result.status == 429 else "MODEL_HTTP_ERROR")
                if result.status not in {408, 429, 500, 502, 503, 504} or attempt == self.config.max_retries:
                    raise ModelError(code, f"Model service returned HTTP {result.status}")
            until = min(deadline, time.monotonic() + 0.25 * (2 ** attempt))
            while time.monotonic() < until:
                if self.cancelled():
                    raise ModelError("CANCELLED", "Model request cancelled during retry delay")
                time.sleep(max(0, min(0.02, until - time.monotonic())))

    def _parse(self, raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            reported_model = data.get("model")
            if isinstance(reported_model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}", reported_model):
                self.reported_models.add(self.redactor.text(reported_model))
            usage = data.get("usage")
            if isinstance(usage, dict):
                keys = ("input_tokens", "output_tokens") if self.config.protocol == "responses" else ("prompt_tokens", "completion_tokens")
                if all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys):
                    self.usage_records.append({"input_tokens": usage[keys[0]], "output_tokens": usage[keys[1]]})
                    if self._pending_reservation is not None:
                        record, self._pending_reservation = self._pending_reservation, None
                        try:
                            self.cost_budget.settle(record, usage[keys[0]], usage[keys[1]])
                        finally:
                            self.cost_observer(phase="usage_received", request=self.requests, budget=self.cost_summary())
            if self.config.protocol == "responses":
                if data.get("status") != "completed" or data.get("error"):
                    raise ModelError("MODEL_INCOMPLETE", "Model response did not complete")
                texts = []
                for item in data["output"]:
                    if item.get("type") == "reasoning":
                        continue  # Never retain reasoning text or encrypted reasoning state.
                    if item.get("type") != "message" or item.get("role") != "assistant":
                        raise ValueError()
                    for part in item["content"]:
                        if part.get("type") == "refusal":
                            raise ModelError("MODEL_REFUSAL", "Model declined this request")
                        if part.get("type") != "output_text":
                            raise ValueError()
                        texts.append(part["text"])
                if len(texts) != 1:
                    raise ValueError()
                text = texts[0]
            else:
                if len(data["choices"]) != 1:
                    raise ValueError()
                choice = data["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ModelError("MODEL_INCOMPLETE", "Model response did not complete")
                message = choice["message"]
                if message.get("refusal"):
                    raise ModelError("MODEL_REFUSAL", "Model declined this request")
                if message.get("tool_calls") or message.get("function_call"):
                    raise ValueError()
                text = message["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError()
            # Parse enough to check string values, but leave action validation/correction to the controller.
            try:
                parsed = json.loads(text)
            except ValueError:
                return "invalid JSON action"  # Do not echo malformed provider content.
            if self.redactor.value(parsed) != parsed:
                raise ModelError("MODEL_SENSITIVE_OUTPUT", "Model action contains credential-like content; action rejected")
            return text
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, ToolError):
                raise
            raise ModelError("MODEL_RESPONSE_FORMAT", "Invalid model response envelope") from exc

    def usage_summary(self):
        if not self.usage_records:
            return "unknown"
        return {"input_tokens": sum(r["input_tokens"] for r in self.usage_records),
                "output_tokens": sum(r["output_tokens"] for r in self.usage_records),
                "requests": self.requests, "unreported_requests": self.requests - len(self.usage_records)}

    def service_summary(self):
        return {"endpoint": self.config.endpoint, "protocol": self.config.protocol,
                "requested_model": self.config.model, "reported_models": sorted(self.reported_models)}

    def cost_summary(self):
        return self.cost_budget.summary()
