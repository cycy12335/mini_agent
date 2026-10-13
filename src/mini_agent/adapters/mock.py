"""Deterministic demonstration, not a general bug-fixing model."""


def call(name, **arguments):
    return {"type": "tool_call", "name": name, "arguments": arguments}


class MockAdapter:
    def __init__(self):
        self.step = 0

    def next_action(self, messages, tool_schemas):
        self.step += 1
        if self.step == 1:
            return call("list_files")
        if self.step == 2:
            return call("read_file", path="src/calculator.py")
        if self.step == 3:
            return call("read_file", path="tests/test_calculator.py")
        if self.step == 4:
            return call("search_text", query="average")
        if self.step == 5:
            source = next((m["result"]["data"] for m in messages
                           if m.get("name") == "read_file" and m["result"]["ok"]
                           and m["result"]["data"]["path"] == "src/calculator.py"), None)
            if source is None:
                return {"type": "final", "message": "Mock 演示未找到示例源码；没有生成修改提案。"}
            original = source["content"]
            expected = ('"""Intentionally buggy fixture for the agent\'s future repair workflow."""\n\n\n'
                        'def average(values: list[float]) -> float:\n'
                        '    """Return the mean; the empty-list bug is intentionally left for repair."""\n'
                        '    return sum(values) / len(values)\n')
            if original.replace("\r\n", "\n") != expected:
                return {"type": "final", "message": "源码不匹配预设 Mock 示例；没有生成修改提案。"}
            newline = "\r\n" if "\r\n" in original else "\n"
            replacement = original.replace("    return sum(values) / len(values)",
                "    if not values:" + newline + "        return 0" + newline + "    return sum(values) / len(values)")
            return call("propose_edits", edits=[{"path": source["path"], "original_sha256": source["sha256"],
                                                "content": replacement}])
        latest = next((m for m in reversed(messages) if m.get("role") == "tool"), {})
        accepted = latest.get("name") == "propose_edits" and latest.get("result", {}).get("ok")
        return {"type": "final", "message": ("Mock 已提出空列表返回 0 的修改建议；尚未应用或验证。" if accepted
                                               else "Mock 提案未被接受；没有修改或验证目标项目。")}
