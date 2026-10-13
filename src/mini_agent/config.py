"""Load non-sensitive budgets. Configuration never grants permissions."""

from dataclasses import dataclass, fields
import json
from pathlib import Path
from .api_config import APIConfig, parse_api_config


class ConfigError(ValueError):
    """An unreadable or invalid configuration file."""


@dataclass(frozen=True)
class AgentConfig:
    api: APIConfig | None = None
    max_actions: int = 20
    model_timeout_seconds: int = 60
    run_timeout_seconds: int = 600
    max_file_bytes: int = 64 * 1024
    max_search_hits: int = 100
    max_edit_files: int = 5
    test_timeout_seconds: int = 60
    max_test_output_bytes: int = 32 * 1024
    max_context_chars: int = 100_000
    max_files: int = 1000
    max_scan_entries: int = 10000
    max_directory_depth: int = 32
    max_tool_output_chars: int = 32768

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "api":
                if value is not None and not isinstance(value, APIConfig):
                    raise ConfigError("api must be validated API configuration")
                continue
            if type(value) is not int or value <= 0:
                raise ConfigError(f"{field.name} must be a positive integer")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def load_config(path: Path | None = None) -> AgentConfig:
    if path is None:
        return AgentConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Do not echo configuration values, which may contain pasted credentials.
        raise ConfigError(f"Cannot read configuration as UTF-8 JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a JSON object")
    unknown = data.keys() - {field.name for field in fields(AgentConfig)}
    if unknown:
        raise ConfigError("Configuration contains unsupported fields")
    if "api" in data:
        try:
            data["api"] = parse_api_config(data["api"])
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    return AgentConfig(**data)
