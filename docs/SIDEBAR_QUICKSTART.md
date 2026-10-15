# VS Code 侧边栏安装与使用

## 安装

先按 [项目首页](../README.md) 安装 Python 核心。插件通过 VSIX 安装；从源码生成 VSIX 需要 Node.js 和 npm。在仓库根目录执行：

```powershell
New-Item -ItemType Directory -Force dist
Set-Location extensions/vscode
npx --yes --package @vscode/vsce@4.0.0 vsce package --no-dependencies --skip-license --out ../../dist/mini-agent-panel.vsix
Set-Location ../..
code --install-extension dist/mini-agent-panel.vsix --force
```

Linux/macOS 将第一行换为 `mkdir -p dist`。也可在 VS Code 扩展菜单中选择“从 VSIX 安装”，打开生成的文件。

安装后在 VS Code 设置中配置：

| 设置 | 用途 |
| --- | --- |
| `miniAgent.pythonPath` | 已安装 Mini Agent 核心的 Python 绝对路径 |
| `miniAgent.testPythonPath` | 目标项目测试虚拟环境的 Python 绝对路径，授权测试模式需要 |
| `miniAgent.configPath` | 可选的 API JSON 配置绝对路径，使用侧边栏配置时可留空 |

Windows 虚拟环境的 Python 位于 `.venv/Scripts/python.exe`，Linux/macOS 位于 `.venv/bin/python`。插件包不携带本机解释器路径或 API Key。

## 配置模型

点击左侧 **Mini Agent** 图标，展开“模型与 API 配置”：

1. 填写服务商的完整请求地址，并选择匹配的 Responses 或 Chat Completions 协议。
2. 填写模型 ID 和 API Key，点击“保存配置”。支持自定义模型与曾用名称。
3. 按服务商价格填写输入、输出单价，可调整运行费用估算上限与推理选项。

Key 保存到 VS Code SecretStorage，保存后输入框清空。留空再次保存会保留 Key，输入新值则替换；“清除面板配置与 Key”会恢复 JSON 配置方式。配置修改对下一次任务生效。

## 运行任务

打开目标项目后，侧边栏会自动填入当前工作区路径。输入问题和预期行为，在“运行选项”中选择：

- **只读分析**：查看定位结果和修改建议，不写入源码或执行测试。
- **确认后修改**：审阅 Diff 并批准后应用修改。
- **授权测试**：使用指定的测试虚拟环境，比较修复前后的测试结果。

真实项目选择 API 模型；Mock 只演示仓库附带的 `examples/buggy_project` 平均值问题。

点击“开始任务”，确认服务及发送范围，等待分析；出现提案后先查看 Diff，再同意或拒绝。可随时点击“取消任务”，或通过“查看运行输出”排查启动问题。

## 历史与恢复

切换到其他侧边栏不会终止任务，再次点击图标可继续查看。关闭整个 VS Code 会中断任务，重新打开后可查看历史记录。

选择历史运行可查看报告、差异、测试结果和定位证据。使用“预览恢复”检查文件清单，确认后恢复；存在后续修改或未保存内容时会阻止覆盖。恢复后应重新运行测试。
