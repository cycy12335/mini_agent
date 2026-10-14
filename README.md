# Mini Coding Agent

一个用于小型 Python 项目的命令行编程助手。输入问题描述和项目目录，它会按需阅读代码、提出修改、展示差异，在你确认后应用补丁并运行测试；需要时可恢复原文件。

- **真实模型修复**：接入 DeepSeek 或兼容的 Responses / Chat Completions API。
- **确认后修改**：发送代码和应用补丁分别征求同意，支持只读分析。
- **测试验证**：先记录基线，再测试修改，区分已修复、剩余和新增失败。
- **备份与恢复**：保存每次修改的差异、原始文件和运行报告。
- **受控运行**：限制文件范围、动作数、运行时间及测试输出；提供每次运行的 API 费用预估预算。

要求 **Python 3.11+**。以下快速开始使用 Windows PowerShell；Linux/macOS 将 `.venv\Scripts\python.exe` 换为 `.venv/bin/python`，复制目录使用 `cp -R`。

## 安装

从 [Releases](https://github.com/cycy12335/mini_agent/releases/latest) 下载并解压 **`mini-coding-agent-0.1.0-quickstart.zip`**。它包含 wheel 安装包、配置模板和示例项目。在解压后的目录中执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\mini_coding_agent-0.1.0-py3-none-any.whl pytest
.\.venv\Scripts\python.exe -m mini_agent --help
```

也可以从源码安装：

```powershell
git clone https://github.com/cycy12335/mini_agent.git
cd mini_agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install . pytest
```

无需激活虚拟环境。安装后也可通过虚拟环境中的 `mini-agent` 命令使用相同功能。

## 配置 API Key

仓库和快速开始包提供空白的 **`agent.credentials.json`**：

```json
{
  "api_key": ""
}
```

推荐复制为本地配置文件，再把 `api_key` 改成平台生成的真实密钥：

```powershell
Copy-Item .\agent.credentials.json .\agent.credentials.local.json
```

```json
{
  "api_key": "在这里粘贴你的 API Key"
}
```

以后直接编辑 `agent.credentials.local.json`，保存后启动即可，无需设置环境变量或重启终端。该文件被 Git 忽略。也支持直接填写 `agent.credentials.json`，但不要提交或分享填入真实密钥的文件。

程序从**启动命令的当前目录**读取配置：优先读取 `agent.credentials.local.json`，不存在时读取 `agent.credentials.json`；所选文件的密钥为空时，回退读取 `MINI_AGENT_API_KEY` 环境变量。密钥以明文保存在本机，不会主动发送到模型上下文或写入运行日志。

服务地址和模型在 **`agent.api.example.json`** 中修改，默认使用 DeepSeek 的 `deepseek-flash`：

```json
{
  "api": {
    "endpoint": "https://api.deepseek.com/responses",
    "model": "deepseek-flash",
    "protocol": "responses"
  }
}
```

上面仅展示关键字段，请直接修改已有配置文件以保留费用预算和请求限制。其他服务需使用受支持的协议，并配置已核对的输入、输出单价。

## 第一次运行

先复制附带的示例，保留原始示例：

```powershell
Copy-Item -Recurse .\examples\buggy_project .\examples\demo
```

在已配置密钥的目录中运行：

```powershell
.\.venv\Scripts\python.exe -m mini_agent run `
  --project .\examples\demo `
  --task "修复 average([]) 崩溃，空列表返回 0，保留非空输入行为" `
  --model api --config .\agent.api.example.json `
  --send-path src --send-path tests `
  --allow-tests --test-python .\.venv\Scripts\python.exe `
  --runs-dir .\.mini-agent\runs
```

程序先展示服务和允许发送的文件。输入 `y` 同意发送后，取得测试基线并开始分析；提出补丁时显示差异，再输入 `y` 才会修改。空输入或 `n` 拒绝。成功后输出 `verified` 和报告路径。

**不使用 API 的演示**：去掉 `--config`、`--send-path`，将 `--model api` 换成 `--model mock`。Mock 不需要密钥、不联网，只演示这个预设平均值示例，不是通用修复模型。

## 使用自己的项目

把 `--project` 换成你的 Python 项目目录，`--task` 写成实际问题描述。项目须已有 `tests/` 测试目录和安装 pytest 的虚拟环境；`--test-python` 指向该环境的 Python。解释器相对路径以启动目录为准。

- `--send-path` 可重复指定允许读取的相对文件或目录；省略时先列出全部合规候选文件供确认。
- `--read-only` 只分析并展示提案，不修改源码、不运行测试；与 `--allow-tests` 互斥。
- 省略 `--allow-tests` 和 `--test-python` 时，仍可审批应用补丁，但结果为 `unverified`。
- 缺少依赖或测试收集失败时会停止并说明原因；请在项目虚拟环境中手动解决后重试。

例如，仅分析：

```powershell
.\.venv\Scripts\python.exe -m mini_agent run `
  --project D:\your-project --task "描述具体错误及预期行为" `
  --model api --config .\agent.api.example.json `
  --send-path src --send-path tests --read-only `
  --runs-dir .\.mini-agent\runs
```

## 查看与恢复

每次运行打印运行编号和报告位置。使用实际编号替换 `<run-id>`：

```powershell
.\.venv\Scripts\python.exe -m mini_agent inspect `
  --runs-dir .\.mini-agent\runs --run-id <run-id>

.\.venv\Scripts\python.exe -m mini_agent rollback `
  --runs-dir .\.mini-agent\runs --run-id <run-id>
```

恢复会先展示文件清单，再请求确认。文件已被人工改动时拒绝覆盖。请保留运行目录中的备份直到不再需要恢复；恢复后的当前状态见 `manifest.json` / `rollback.json`，原报告保留历史结果。

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `verified` | 当前修改经过实际测试和基线比较 | 0 |
| `proposed` | 仅生成未验证的分析或提案 | 0 |
| `unverified` | 已应用修改，但未运行测试 | 2 |
| `failed` | 测试、模型请求、权限检查或预算失败 | 1 |
| `cancelled` | 拒绝操作或主动取消 | 3 |

## 只读文件工具

不需要模型或密钥：

```powershell
.\.venv\Scripts\python.exe -m mini_agent files --project D:\your-project list
.\.venv\Scripts\python.exe -m mini_agent files --project D:\your-project read src/example.py
.\.venv\Scripts\python.exe -m mini_agent files --project D:\your-project search "keyword"
```

结果为 JSON，包含成功状态、数据、错误代码及截断标记。

## 使用边界

当前仅替换已有 Python 源码和测试文件，不自动创建文件、安装依赖、修改环境配置或执行 Git 操作。模型不能指定任意终端命令。测试命令固定为 `python -m pytest tests -q`。

仅对信任的项目授权测试：执行测试会运行项目代码，执行器不是操作系统沙箱。发送前请检查服务地址和文件范围；敏感文件过滤及脱敏不能发现所有自定义秘密。

默认每次完整运行的 API 费用预估上限为 **¥0.5**，包含所有模型请求和重试。超出预估预算前停止发送；该限制基于配置单价和 token 估计，不能代替服务端账户额度或实际账单。

`verified` 只覆盖实际运行的测试。多文件修改不具有跨文件原子性，硬中断后请检查运行清单并执行恢复。