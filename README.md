# Mini Agent

面向 Python 项目的 AI 编程助手，支持 VS Code 侧边栏和命令行。描述问题后，Mini Agent 会阅读代码、定位原因、展示修改建议，经你确认后应用补丁并运行测试。

## 功能

- **代码分析**：识别项目结构，查询 Python 符号、引用及调用关系。
- **定位与修复**：记录定位证据，提出单文件或多文件修改，支持新增回归测试。
- **确认后执行**：发送代码、应用修改和恢复文件均需确认，支持只读分析。
- **测试与恢复**：比较修复前后的测试结果，保存差异、报告和文件备份。
- **模型配置**：支持 Responses 和 Chat Completions 兼容接口，可设置运行及费用估算上限。

## 安装

需要 Python 3.11+。在 PowerShell 中执行：

```powershell
git clone https://github.com/cycy12335/mini_agent.git
cd mini_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install . pytest
```

Linux/macOS 使用 `source .venv/bin/activate` 激活环境，文件复制使用 `cp`，目录复制使用 `cp -R`。目标项目的测试依赖需安装在其测试虚拟环境中。

## VS Code 使用

按 [侧边栏安装与使用](docs/SIDEBAR_QUICKSTART.md) 安装插件，并设置已安装 Mini Agent 的 Python 路径。

1. 在 VS Code 打开目标项目，点击左侧 **Mini Agent** 图标。
2. 在“模型与 API 配置”填写请求地址、协议、模型 ID 和 API Key，保存配置。Key 使用 VS Code SecretStorage 保存。
3. 输入问题描述，选择只读分析、确认后修改或授权测试模式，点击“开始任务”。
4. 查看定位结果和 Diff，确认后继续；可随时取消或查看历史记录。

## 命令行使用

复制凭据模板，并在新文件中填入 API Key：

```powershell
Copy-Item agent.credentials.json agent.credentials.local.json
```

在 `agent.api.example.json` 中配置完整请求地址、模型、协议及输入/输出单价。随后从当前目录启动只读分析：

```powershell
python -m mini_agent run --project ../your-project --task "描述问题与预期行为" --model api --config agent.api.example.json --read-only
```

需要修改并验证时，将 `--read-only` 替换为 `--allow-tests --test-python ../your-project/.venv/Scripts/python.exe`；Linux/macOS 使用 `.venv/bin/python`。运行时会先确认发送范围，再展示补丁并请求修改确认。

不使用 API 的示例：

```powershell
Copy-Item -Recurse examples/buggy_project examples/demo
python -m mini_agent run --project examples/demo --task "分析空列表求平均值时的错误" --model mock --read-only
```

Mock 仅演示附带的平均值示例，不需要密钥或网络。

## 查看与恢复

运行结束后会显示运行编号及报告位置：

```powershell
python -m mini_agent inspect --run-id <run-id>
python -m mini_agent rollback --run-id <run-id>
```

使用自定义 `--runs-dir` 时，查看和恢复命令也需指定同一目录。恢复前会检查文件是否被后续修改，并请求确认。

`verified` 表示修改通过了实际执行的测试；未执行测试的修改显示为 `unverified`。测试需在可信项目中运行，默认每次 API 运行的费用估算上限为 ¥0.5，实际费用以服务商账单为准。

更多命令见 `python -m mini_agent --help`。集成接口见 [本地任务服务](docs/CORE_SERVICE.md) 与 [面板协议](docs/PANEL_PROTOCOL.md)。
