"""Bound serialized context by dropping oldest complete action/result groups."""

import json


class ContextLimitError(ValueError):
    pass


def bounded_context(messages: list[dict], limit: int) -> list[dict]:
    kept = list(messages)
    while len(json.dumps(kept, ensure_ascii=False)) > limit:
        remove_count = 2 if len(kept) > 2 and kept[2].get("role") == "tool" else 1
        if len(kept) - remove_count <= 1:
            raise ContextLimitError("Task and newest result exceed max_context_chars")
        # First message contains immutable instructions/task. Drop one old turn.
        del kept[1:1 + remove_count]
    return kept
