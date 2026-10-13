"""Strict validation before any model-directed tool dispatch."""

import json
import re

from .config import AgentConfig


class ActionError(ValueError):
    pass


ARGUMENTS = {
    "list_files": ({}, {"path": str}),
    "read_file": ({"path": str}, {}),
    "search_text": ({"query": str}, {"path": str}),
    "propose_edits": ({"edits": list}, {}),
    "run_tests": ({}, {}),
}
TOOL_SCHEMAS = [
    {"name": name, "parameters": {
        "type": "object", "required": list(required), "additionalProperties": False,
        "properties": {key: {"type": "array" if kind is list else "string"}
                       for key, kind in (required | optional).items()},
    }} for name, (required, optional) in ARGUMENTS.items()
]
TOOL_SCHEMAS[3]["parameters"]["properties"]["edits"]["items"] = {
    "type": "object", "additionalProperties": False,
    "required": ["path", "original_sha256", "content"],
    "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                   "original_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ActionError("Duplicate JSON fields are not allowed")
        result[key] = value
    return result


def _text(value, maximum, name, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ActionError(f"Invalid {name}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionError(f"Invalid UTF-8 in {name}") from exc


def validate_action(raw, config: AgentConfig) -> dict:
    if isinstance(raw, str):
        if len(raw) > config.max_file_bytes * config.max_edit_files * 6 + 16384:
            raise ActionError("Action exceeds size limit")
        try:
            raw = json.loads(raw, object_pairs_hook=_object)
        except (ValueError, RecursionError) as exc:
            raise ActionError("Expected one JSON action object") from exc
    if not isinstance(raw, dict):
        raise ActionError("Expected one action object")
    if raw.get("type") == "final":
        if set(raw) != {"type", "message"}:
            raise ActionError("Final action requires only type and message")
        _text(raw["message"], 4096, "final message")
        return raw
    if raw.get("type") != "tool_call" or set(raw) != {"type", "name", "arguments"}:
        raise ActionError("Expected type, name and arguments")
    name, args = raw["name"], raw["arguments"]
    if not isinstance(name, str) or name not in ARGUMENTS:
        raise ActionError("Unknown tool")
    required, optional = ARGUMENTS[name]
    if not isinstance(args, dict) or not required.keys() <= args.keys() or args.keys() - (required.keys() | optional.keys()):
        raise ActionError("Missing or unexpected tool arguments")
    for key, value in args.items():
        if not isinstance(value, (required | optional)[key]):
            raise ActionError("Invalid tool argument type")
        if key in {"path", "query"}:
            _text(value, 1024 if key == "path" else 256, key)
    if name == "propose_edits":
        edits = args["edits"]
        if not 1 <= len(edits) <= config.max_edit_files:
            raise ActionError("Proposal file count exceeds limits")
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"path", "original_sha256", "content"}:
                raise ActionError("Edit requires path, original_sha256 and content")
            _text(edit["path"], 1024, "edit path")
            _text(edit["content"], config.max_file_bytes, "edit content", empty=True)
            if len(edit["content"].encode("utf-8")) > config.max_file_bytes:
                raise ActionError("Replacement content exceeds file limit")
            if not isinstance(edit["original_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", edit["original_sha256"]):
                raise ActionError("Expected lowercase SHA-256")
    return raw
