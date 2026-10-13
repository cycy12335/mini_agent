"""Explicit non-secret API configuration; credentials never belong in JSON."""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class APIConfig:
    endpoint: str
    model: str
    protocol: str = "responses"
    api_key_env: str = "MINI_AGENT_API_KEY"
    max_output_tokens: int = 8192
    max_response_bytes: int = 1024 * 1024
    max_retries: int = 2
    reasoning_effort: str | None = None

    def __post_init__(self):
        if self.protocol not in {"responses", "chat_completions"}:
            raise ValueError("Unsupported API protocol")
        if not isinstance(self.endpoint, str) or len(self.endpoint) > 2048 or any(ord(c) < 33 for c in self.endpoint):
            raise ValueError("Invalid API endpoint")
        try:
            url = urlsplit(self.endpoint)
            port = url.port
            valid = (url.hostname and not url.username and not url.password and not url.query and not url.fragment
                     and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"})))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("API endpoint must be HTTPS without credentials, query or fragment (loopback HTTP allowed)")
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}", self.model):
            raise ValueError("Invalid API model name")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.api_key_env):
            raise ValueError("Invalid API key environment variable name")
        for name in ("max_output_tokens", "max_response_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 2:
            raise ValueError("max_retries must be 0, 1 or 2")
        if self.reasoning_effort is not None and self.reasoning_effort not in {"none", "low", "medium", "high", "max"}:
            raise ValueError("Unsupported reasoning_effort")


def parse_api_config(data):
    if not isinstance(data, dict):
        raise ValueError("api must be a non-secret configuration object")
    try:
        return APIConfig(**data)
    except TypeError as exc:
        raise ValueError("API configuration has missing or unsupported fields") from exc
