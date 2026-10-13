import json

import pytest

from mini_agent.actions import ActionError, validate_action
from mini_agent.adapters.mock import call
from mini_agent.config import AgentConfig


@pytest.mark.parametrize("action", ["not json", "[]", '{"type":"final","type":"final","message":"x"}',
    {"type": "final", "message": "x", "status": "verified"},
    {"type": "final", "message": "x" * 4097},
    call("shell", command="echo nope"), call([], command="nope"),
    call("read_file"), call("read_file", path=3), call("read_file", path=""),
    call("read_file", path="x" * 1025), call("list_files", extra=True),
    call("run_tests", command="python"), call("propose_edits", edits=[]),
    call("propose_edits", edits=[{"path": "a.py", "content": "x", "original_sha256": "bad"}]),
    call("propose_edits", edits=[{"path": "a.py", "content": "\ud800", "original_sha256": "a" * 64}]),
])
def test_invalid_actions_are_rejected(action):
    with pytest.raises(ActionError):
        validate_action(action, AgentConfig())


def test_valid_actions_and_json():
    action = call("read_file", path="src/a.py")
    assert validate_action(json.dumps(action), AgentConfig()) == action
    assert validate_action({"type": "final", "message": "done"}, AgentConfig())["message"] == "done"


def test_proposal_bytes_and_file_count():
    edit = {"path": "a.py", "original_sha256": "a" * 64, "content": "你你"}
    with pytest.raises(ActionError):
        validate_action(call("propose_edits", edits=[edit]), AgentConfig(max_file_bytes=5))
    with pytest.raises(ActionError):
        validate_action(call("propose_edits", edits=[edit] * 6), AgentConfig())
