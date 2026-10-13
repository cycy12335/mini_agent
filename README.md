# Mini Coding Agent

遵循 [PLAN.md](PLAN.md) 的 Python 命令行编程 Agent。当前完成阶段 0–2：包结构、只读文件工具与权限边界、离线 Mock 循环、提案记录和审计报告。

`files` 可用于浏览目标项目，`run --model mock` 可跑通预设示例的“列表 → 读取源码 → 读取测试 → 搜索 → 提案 → 结束”。Mock 是固定演示，不是通用模型；任务描述会记录在会话中，但不会改变预设修复策略。尚未实现补丁差异预览、审批、应用与恢复、项目测试执行器及真实模型 API。`inspect` 和 `rollback` 将在后续阶段添加。

## 环境与安装

要求 Python 3.11+，本次开发使用 Windows / Python 3.14.6，实际验证的 pytest 版本为 9.1.1。运行时代码仅依赖标准库。

在 PowerShell 的项目根目录中执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m mini_agent --help
.\.venv\Scripts\python.exe -m mini_agent run --help
```

VS Code 中通过 `Python: Select Interpreter` 选择 `.venv\Scripts\python.exe`。无需激活环境或修改 PowerShell 执行策略。

## 阶段 0 验收

Agent 自身的测试应全部通过：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

若受限环境无法访问系统 pytest 临时目录，可将测试临时文件放在工作区（本次验收使用此方式）：

```powershell
New-Item -ItemType Directory -Force .pytest-tmp | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = (Resolve-Path .pytest-tmp).Path
.\.venv\Scripts\python.exe -m pytest tests -q
```

示例项目故意保留 `average([])` 的除零错误，测试期望空列表返回 0。单独执行它应得到 **1 failed, 1 passed**，退出码 **1**：

```powershell
.\.venv\Scripts\python.exe -m pytest examples/buggy_project/tests -q
```

普通 `pytest` 只收集根目录 `tests/`，示例基线失败不混入 Agent 单元测试。不要将示例测试改成跳过或预期失败；它是后续修复流程的验收输入。后续修复演示应使用该示例的副本。

## 参数与配置

```powershell
.\.venv\Scripts\python.exe -m mini_agent run `
  --project .\examples\buggy_project `
  --task "average 在空列表时崩溃，期望返回 0" `
  --model mock `
  --config .\agent.config.example.json `
  --read-only
```

此命令运行离线 Mock 演示，正常示例结束为 `proposed`，退出码 0；这表示建议未应用、未验证。阶段 2 无论是否传入 `--read-only` 都只读目标项目；传入 `--allow-tests`、`--test-python` 或 `--model api` 会明确拒绝，不会启动项目测试或网络调用。项目根仍通过完整路径权限检查，不会先消除符号链接再校验。

产物默认保存在 Agent 项目的 `.mini-agent/runs/<run-id>/`：`events.jsonl` 记录动作、工具结果、格式错误和结束事件；`proposals.json` 保存已接受的完整文件替换提案（没有提案时不生成）；`summary.json` 与 `report.md` 列出状态、已读文件、提案数及未验证限制。日志可能包含源码，默认被 Git 忽略。源码中的提示文本只作为数据，不改变工具权限。

可用 `--runs-dir <目录>` 指定审计根目录，必须在目标项目之外，不能经由符号链接或重解析点访问；该路径只由用户配置，模型不能指定。源码安装使用上述默认目录；安装到其他位置时可显式选择自己的可写产物目录。

模型每次只能返回一个动作：

```json
{"type": "tool_call", "name": "read_file", "arguments": {"path": "src/calculator.py"}}
```

结束动作是 `{"type":"final","message":"结论"}`。提案工具 `propose_edits` 接受 `edits` 数组，每项只有 `path`、`original_sha256` 和完整 UTF-8 `content`；仅允许已有 Python 源码/测试文件，禁止 `setup.py` 和配置文件。路径、大小、原始哈希和重复路径均须校验。阶段 2 只保存提案，不表示已审批；差异预览与写入事务留到阶段 3。

非法 JSON、未知工具、缺参、多余字段或不合法类型最多允许一次纠正；再次无效结束为 `failed`。每次模型调用（包括无效动作和 final）占用一步，默认最多 20 步。支持 Ctrl+C 取消（`cancelled`，退出码 3）、步数/上下文/运行时间超限和执行错误（`failed`，退出码 1）。`proposed` 仅表示已有读取依据的未验证结论，是否生成提案应查看报告中的提案数。模型文字不决定测试状态：当前报告始终为 `not_run`，不会输出控制器认定的 `verified`。

时间预算在调度前和适配器返回后检查；当前 Mock 是即时本地逻辑，没有后台线程或进程。同步调用的超时检查不能强制中断一个不返回的自定义 Python 适配器；真实 API 的网络请求超时与重试将在阶段 5 实现。上下文超限时移除最旧的完整动作/结果组；任务和最新结果仍超限则明确停止，不静默丢弃最新结果。

配置使用 UTF-8 JSON，可只覆盖部分预算；未知字段、重复字段、非正整数、布尔值与格式错误均被拒绝。文件、搜索、扫描、动作、提案大小和上下文预算已生效，测试预算待阶段 4 实现。模型服务地址和认证协议将在阶段 5 确定，当前不接受密钥字段，也不读取环境中的模型凭据。

`.env.example` 仅为未来环境变量的占位说明，程序不会自动加载 `.env`。不要在配置或命令行中保存真实密钥。

## 只读工具（阶段 1）

```powershell
.\.venv\Scripts\python.exe -m mini_agent files --project .\examples\buggy_project list
.\.venv\Scripts\python.exe -m mini_agent files --project .\examples\buggy_project read src/calculator.py
.\.venv\Scripts\python.exe -m mini_agent files --project .\examples\buggy_project search average
```

列表与搜索支持 `--path src` 缩小到相对目录。所有工具输出统一 JSON：`ok`、`data`、`error_code`、`message`、`truncated`。工具成功返回 0，工具失败返回 1，CLI 参数/配置错误返回 2。`read` 返回内容、字节数和原始文件 SHA-256；搜索为区分大小写的字面量匹配，返回路径、行号及最多 500 字符的片段，长行标记 `line_truncated`。

工具只读取明确允许的 `.py/.pyi/.md/.rst/.txt/.toml/.json/.yaml/.yml/.ini/.cfg` 文件。列表返回符合路径规则的候选文件；读取和搜索额外检查 UTF-8、控制字符和大小。默认排除隐藏路径、依赖/构建目录、凭据/密钥相关文件名；拒绝路径穿越、绝对路径、Windows 盘符/UNC/设备名/备用数据流、符号链接、junction、其他重解析点和硬链接。目标目录不能覆盖 Agent 自身代码，示例子目录可用。

默认上限：读取 64 KiB、搜索 100 条、文件列表 1000 个、扫描 10000 个目录项、目录深度 32；列表路径和搜索片段文本合计上限为 32768 字符（不含 JSON 字段开销）。达到扫描或结果上限时标记 `truncated`。被路径规则过滤的条目以及搜索中无法读取的候选文件计入 `skipped`，因此无匹配不表示被跳过的文件也已检查。

阶段 1 不存在写入、命令执行或模型调用入口。文件名过滤不能识别任意源码中内嵌的秘密；这些检查不能抵御恶意并发文件系统替换，也不是操作系统沙箱。真实模型发送范围与脱敏将在后续阶段实现。

## CI

[GitHub Actions 工作流](.github/workflows/ci.yml) 在 push、pull request 或手动触发时运行，覆盖 Windows/Linux 与 Python 3.11、3.12、3.13、3.14。每个组合执行可编辑安装、CLI 检查、Agent 测试及示例基线检查，工作流只有仓库读取权限。

`python scripts/check_example_baseline.py` 验证示例准确产生一个除零失败和一个正常通过；其他失败、测试被跳过、测试集变化或缺陷意外消失均使检查失败。它是开发/CI 脚本，不是提供给模型的测试工具。

工作流使用方式参考官方 [checkout](https://github.com/actions/checkout) 与 [setup-python](https://github.com/actions/setup-python) 文档。

## 验收与后续开发

2026-10-13 阶段 0 验收记录：全新 `.venv` 可编辑安装成功；模块 `--help` 退出码 0；Agent 测试 18 passed；示例基线 1 failed、1 passed，失败原因为 `average([])` 的 `ZeroDivisionError`；携带示例配置的 `run` 按设计报告未实现，退出码 1。

2026-10-13 阶段 1 本地验收：73 passed、2 skipped。Windows junction 和硬链接检查通过；本机无符号链接创建权限，因此该测试跳过；POSIX FIFO 测试仅在 Linux 运行。示例基线检查通过，保留预期的空列表除零错误。跨平台结果以 GitHub Actions 实际运行为准。

2026-10-13 阶段 2 本地验收：110 passed、1 skipped（Windows 不执行 POSIX FIFO）；真实符号链接与 junction 检查通过。实际 CLI 以 6 次动作完成 Mock 提案流程，返回 `proposed`；示例基线仍为准确的 1 failed、1 passed。提案与日志保存在本地运行目录，目标源码未应用修改。

下一步为阶段 3：统一差异、逐次审批、备份、哈希冲突检测、应用和恢复。阶段 2 的离线流程及异常动作、权限拒绝、预算终止、取消和审计验证均纳入 `tests/`，由已有跨平台 CI 自动执行。
