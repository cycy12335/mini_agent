from typing import Protocol


class ModelAdapter(Protocol):
    def next_action(self, messages: list[dict], tool_schemas: list[dict]) -> dict | str:
        """Return one internal tool_call or final action, never executable code."""
        ...
