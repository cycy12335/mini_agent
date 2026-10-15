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
    "propose_edits": ({"edits": list}, {"reason": str, "evidence_ids": list, "test_change_reason": str}),
    "run_tests": ({}, {}),
    "find_definition": ({"symbol": str}, {"path": str}),
    "find_references": ({"symbol": str}, {"path": str}),
    "find_callers": ({"symbol": str}, {"path": str}),
    "find_callees": ({"symbol": str}, {"path": str}),
    "find_related_tests": ({"symbol": str}, {"path": str}),
    "record_problem": ({"expected_behavior": str, "trigger": str, "observed_behavior": str}, {"assumptions": list}),
    "propose_hypothesis": ({"claim": str, "evidence_ids": list}, {}),
    "record_check": ({"hypothesis_id": str, "outcome": str, "finding": str, "evidence_ids": list}, {}),
    "conclude_root_cause": ({"hypothesis_id": str, "conclusion": str}, {"unresolved": list}),
}
DIAGNOSIS_TOOLS = frozenset({"record_problem", "propose_hypothesis", "record_check", "conclude_root_cause"})
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
                   "original_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
                   "operation": {"type": "string", "enum": ["replace", "create"]}},
}
TOOL_SCHEMAS[3]["parameters"]["properties"]["evidence_ids"]["items"] = {"type": "string"}
for schema in TOOL_SCHEMAS:
    if schema["name"] in DIAGNOSIS_TOOLS:
        for name, property_schema in schema["parameters"]["properties"].items():
            if property_schema["type"] == "array":
                property_schema["items"] = {"type": "string"}
        if schema["name"] == "record_check":
            schema["parameters"]["properties"]["outcome"]["enum"] = ["supported", "rejected", "inconclusive"]


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
        if key in {"path", "query", "symbol"}:
            _text(value, 1024 if key == "path" else 256, key)
            if key == "symbol" and any(char in value for char in "\n\r\t"):
                raise ActionError("Symbol must be on one line")
    if name in DIAGNOSIS_TOOLS:
        for key, value in args.items():
            if isinstance(value, str):
                _text(value, 32 if key in {"hypothesis_id", "outcome"} else 2048, key)
            else:
                if len(value) > 16:
                    raise ActionError("Diagnosis list exceeds limits")
                for item in value:
                    _text(item, 32 if key == "evidence_ids" else 512, key)
                if len(set(value)) != len(value):
                    raise ActionError("Duplicate diagnosis entries")
        if name == "record_check" and args["outcome"] not in {"supported", "rejected", "inconclusive"}:
            raise ActionError("Invalid check outcome")
    if name == "propose_edits":
        for key in {"reason", "test_change_reason"} & args.keys():
            _text(args[key], 2048, key)
        if "evidence_ids" in args:
            if len(args["evidence_ids"]) > 16:
                raise ActionError("Too many repair evidence IDs")
            for identity in args["evidence_ids"]:
                _text(identity, 32, "evidence ID")
            if len(set(args["evidence_ids"])) != len(args["evidence_ids"]):
                raise ActionError("Duplicate repair evidence IDs")
        edits = args["edits"]
        if not 1 <= len(edits) <= config.max_edit_files:
            raise ActionError("Proposal file count exceeds limits")
        for edit in edits:
            if not isinstance(edit, dict) or not {"path", "original_sha256", "content"} <= edit.keys() or edit.keys() - {"path", "original_sha256", "content", "operation"}:
                raise ActionError("Edit requires path, original_sha256 and content")
            operation = edit.get("operation", "replace")
            if not isinstance(operation, str) or operation not in {"replace", "create"}:
                raise ActionError("Invalid edit operation")
            _text(edit["path"], 1024, "edit path")
            _text(edit["content"], config.max_file_bytes, "edit content", empty=True)
            if len(edit["content"].encode("utf-8")) > config.max_file_bytes:
                raise ActionError("Replacement content exceeds file limit")
            if operation == "create" and edit["original_sha256"] is not None:
                raise ActionError("New files require original_sha256=null")
            if operation == "replace" and (not isinstance(edit["original_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", edit["original_sha256"])):
                raise ActionError("Expected lowercase SHA-256")
    return raw
