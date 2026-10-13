import json

import pytest

from mini_agent.config import AgentConfig, ConfigError, load_config


def test_default_and_partial_config(tmp_path):
    assert load_config() == AgentConfig()
    path = tmp_path / "config.json"
    path.write_text('{"max_actions": 3}', encoding="utf-8-sig")
    config = load_config(path)
    assert config.max_actions == 3
    assert config.max_file_bytes == 65536


@pytest.mark.parametrize("data", [[], None, {"api_key": "secret"},
                                  {"max_actions": 0}, {"max_actions": -1},
                                  {"max_actions": True}, {"max_actions": "20"},
                                  {"max_actions": 1.5}])
def test_rejects_invalid_configuration(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("content", ['{"max_actions":',
                                     '{"max_actions": 1, "max_actions": 2}'])
def test_rejects_malformed_or_duplicate_json(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_config(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.json")
