# Mini Coding Agent

最新交付验收见 [完整验收报告（2026-10-15）](docs/FULL_ACCEPTANCE_2026-10-15.md)：自动检查通过，真实 API 由用户自行验证通过。


VS Code 用户可直接点击左侧 **Mini Agent 机器人图标**，在当前项目窗口输入问题；侧边栏自动使用工作区路径。安装与操作见 [侧边栏快速开始](docs/SIDEBAR_QUICKSTART.md)，无需进入插件目录或启动开发宿主。

一个用于小型 Python 项目的命令行编程助手。输入问题描述和项目目录，它会按需阅读代码、提出修改、展示差异，在你确认后应用补丁并运行测试；需要时可恢复原文件。

- **真实模型修复**：接入 DeepSeek 或兼容的 Responses / Chat Completions API。
- **确认后修改**：发送代码和应用补丁分别征求同意，支持只读分析。
- **测试验证**：先记录基线，再测试修改，区分已修复、剩余和新增失败。
- **备份与恢复**：保存每次修改的差异、原始文件和运行报告。
- **受控运行**：限制文件范围、动作数、运行时间及测试输出；提供每次运行的 API 费用预估预算。

要求 **Python 3.11+**。以下快速开始使用 Windows PowerShell；Linux/macOS 将 `.venv\Scripts\python.exe` 换为 `.venv/bin/python`，复制目录使用 `cp -R`。

## V2 与可视化

两条开发路线分别记录在 [可视化实施计划](docs/VISUALIZATION_PLAN.md) 和 [Agent 核心 V2 计划](docs/AGENT_V2_PLAN.md)。V2 已实现 Core-1 仓库探测、Core-2 Python AST 查询、Core-3 结构化定位和 Core-4 修复增强：追踪代码、保存检查证据，再提出带依据的多文件修复和新回归测试，检查冲突与静态导入一致性。VS Code 面板接收自然语言调试请求，通过 stdio 适配层运行独立核心，提供面板审批、原生 Diff、取消、审计时间线与报告。原有 CLI 仍可独立使用，开发启动方式见 [插件说明](extensions/vscode/README.md)。

## 安装

可视化 UI-4（插件 0.4.0）现已提供相关调用图、定位假设与证据、代码跳转、费用预算及只读记录导入，沿用当前 Python 核心接口。离线验收步骤见 [人工指南 F/G](docs/VISUALIZATION_MANUAL_TEST.md)，实现边界见 [UI-4 说明](docs/VISUALIZATION_UI4.md)。

当前工作区的新版本为 **0.7.0**，已包含 Core-1 至 Core-6、独立任务服务与候选集验收工具。本地更新旧版 wheel：

```powershell
Set-Location D:\mini_agent
.\.venv\Scripts\python.exe -m pip install --no-index --force-reinstall --no-cache-dir .\dist\mini_coding_agent-0.7.0-py3-none-any.whl
.\.venv\Scripts\python.exe -m mini_agent --version
```

预期版本为 `0.7.0`。验收工具使用见 [ACCEPTANCE_V2.md](docs/ACCEPTANCE_V2.md)；可视化人工测试请按 [逐步测试指南](docs/VISUALIZATION_MANUAL_TEST.md) 操作。此 wheel 更新为本地交付；下面保留旧发布包的安装说明。

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

把 `--project` 换成你的 Python 项目目录，`--task` 写成实际问题描述。测试需要安装 pytest 和项目依赖的虚拟环境；核心读取 pytest 配置中的相对 `testpaths`，没有配置时选择 `tests/`，不存在该目录时从项目根收集。提供 `--allow-tests` 后可省略 `--test-python`：核心在项目内探测 `.venv`、`venv`、`env`、`.env`，仅在找到唯一环境时自动选择；缺失或存在多个候选时报告原因，需显式指定解释器。显式解释器的相对路径以启动目录为准。

- `--send-path` 可重复指定允许读取的相对文件或目录；省略时自动建立合规候选清单供确认。模型概览仅包含该范围内的元数据，源码仍按需读取。
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

## 仓库自动探测

只提供项目路径即可离线获得 JSON 概览，无需模型密钥，不运行项目代码：

```powershell
.\.venv\Scripts\python.exe -m mini_agent project --project D:\your-project
```

概览包含源码根和模块候选、测试文件、pyproject/pytest/setup.cfg/requirements 声明、本地虚拟环境和合规路径的 Git 修改。概览中的模块名按路径和 setuptools 声明推断；AST 关系通过以下符号工具按需查询。Git 不可用、配置解析失败、无测试或环境不明确时会给出状态或提示。扫描和输出均受预算限制，截断会明确标记。每次 `run` 保存 `repository.json`，`inspect` 可读取；不会读取未保存的编辑器内容或自动安装依赖。

## Python 符号与调用查询

离线查询无需模型密钥，不执行项目代码：

```powershell
.\.venv\Scripts\python.exe -m mini_agent symbols --project D:\your-project definition checkout
.\.venv\Scripts\python.exe -m mini_agent symbols --project D:\your-project callers checkout
.\.venv\Scripts\python.exe -m mini_agent symbols --project D:\your-project callees checkout
.\.venv\Scripts\python.exe -m mini_agent symbols --project D:\your-project references checkout
.\.venv\Scripts\python.exe -m mini_agent symbols --project D:\your-project related-tests checkout
```

运行中的模型可使用 `find_definition`、`find_references`、`find_callers`、`find_callees` 和 `find_related_tests`。`symbol` 支持短名、完整名称或返回的符号 ID；同名定义会标记 `ambiguous`，可用 ID 或 `--path src/service.py` 限定目标定义文件。引用/调用结果含调用者 ID、目标 ID、文件、行号、UTF-8 字节列偏移和源码 SHA-256。支持普通/异步函数、类、方法、导入别名、相对导入及重导出；每次查询检查文件哈希，仅重新解析变化的文件，删除、语法错误和敏感内容会移除旧缓存。

AST 查询沿用文件过滤和已确认发送范围，在首次查询时建立索引。动态接收者、装饰器、条件定义、运行时导入和延迟表达式返回不确定标记；反射、任意回调和运行时导入无法完整静态解析，需执行证据补充。相关测试是最多 8 层反向引用链的候选，不代表实际测试覆盖，也不会自动执行。查询的 `coverage` 报告索引范围、解析错误和完整性；`complete` 仅表示允许范围内的静态索引是否完成，不保证所有运行时调用已被发现。

默认索引预算为 4 MiB 源码、100000 个 AST 节点（`max_index_bytes`、`max_index_nodes`），同时沿用文件扫描、单文件、匹配数和输出预算。预算或查询输出被截断时返回 `truncated`；工具结果继续记录在运行审计事件中。

从源码更新后，请重新安装项目以使用新增命令；开发验证也可先设置 `$env:PYTHONPATH = (Resolve-Path src).Path`，让当前 PowerShell 会话加载工作区源码。

## 结构化定位记录

模型可通过 `record_problem` 记录预期行为、触发条件和当前错误，通过 `propose_hypothesis` 提出候选原因。代码查询和授权测试返回证据 ID；提出假设后，需要一次新的源码查询或测试，再通过 `record_check` 保存支持、排除或未确认的检查结果。`conclude_root_cause` 只接受已支持且证据仍有效的假设。模型不能自行提供测试结果或伪造来源；涉及文件变化时需要重新收集证据。

每步保存 `diagnosis.json`，其中包含源码位置和哈希、执行记录引用、假设和检查。`inspect`、报告及 CLI 同时展示诊断状态和未确认项；取消、模型失败或预算耗尽时保留已有进度。缺少触发条件等信息可记录为假设条件，结束时会列为限制。默认最多 8 个假设、100 条证据、32768 字符诊断状态（`max_hypotheses/max_evidence/max_diagnosis_chars`）。

`evidence_supported` 表示模型根因结论引用了已校验的代码或执行证据，因果判断仍是模型结论；源码被授权补丁修改后标为 `supported_before_edit` 并保留历史依据。诊断状态与修复结果分开：测试仍失败时，定位到根因的运行仍报告 `failed`；未运行测试不会宣称修复已验证。既有 Mock 只演示预设修复，未形成结构化根因时会明确显示未确认。

## 只读文件工具

不需要模型或密钥：

```powershell
.\.venv\Scripts\python.exe -m mini_agent files --project D:\your-project list
.\.venv\Scripts\python.exe -m mini_agent files --project D:\your-project read src/example.py
.\.venv\Scripts\python.exe -m mini_agent files --project D:\your-project search "keyword"
```

结果为 JSON，包含成功状态、数据、错误代码及截断标记。

## 使用边界

支持替换已有 Python 源码/测试及在已有目录内创建 Python 文件；不创建目录、不安装依赖、不修改环境配置或执行 Git 写入。新/多文件修复要求原因和当前源码证据，先检查语法和所改模块的命名导入一致性，再请求审批。创建审批同时授权后续按需读取所列新增文件；拒绝审批不会写入或扩大范围。

新增文件的回滚会恢复“原先不存在”的状态，文件已被用户修改时拒绝删除。测试预期调整仅支持等值比较右侧的字面量，并要求明确理由与第二次独立审批；既有断言结构不能弱化、删除或跳过。每次运行默认最多 3 次修复尝试（`max_repair_attempts`），文件或依赖变化需重新取证并提交新提案，旧审批不会复用。VS Code 未保存路径会传到核心写入/恢复检查，CLI 无法感知编辑器缓冲区。

静态检查覆盖允许读取范围内的命名导入与所改模块导出，动态调用和完整运行语义仍依赖实际测试。模型不能指定任意终端命令或测试参数；核心构建 `python -m pytest -c <项目配置或受控空配置> --rootdir=. <验证过的路径/节点> -q`。验证计划和执行范围见 `validation.json`。

## 自动验证计划

授权测试后，核心记录基线，再依据基线失败、AST 相关测试及补丁中的新测试生成计划。批准包含新回归用例的补丁后，核心先复制策略允许的文本文件到运行目录中的受控工作区，只放入新测试，保留修复前源码。实际新用例必须失败；提前通过、跳过、设置/清理错误、缺依赖或缺文件不算复现，目标仓库不会写入。副本及原仓库的源码变化也会使预修复证明失效。

修复后依次验证目标测试、受影响测试和配置指定的完整测试集；小项目的重复范围会合并。目标失败时停止扩大范围并提供有限修复迭代反馈。没有既有测试时保存真实的空收集基线，仍需通过新回归用例证明修复前失败、修复后通过。缺依赖先报告阻塞，不自动安装。

`validation.json` 保存计划、阶段、实际参数、回归失败用例和产物引用，`inspect` 与报告可查看。只有完整、当前源码稳定且基线对比有效的执行证据才可报告 `verified`；部分成功或测试后源码改变保持未验证。验证只覆盖实际执行的配置测试范围，不保证仓库所有行为正确。默认最多 100 个测试选择器、8 MiB 预修复副本文本（`max_test_targets/max_validation_copy_bytes`）；副本不包含被策略过滤的资源，因此不适用的复现会明确阻塞。

仅对信任的项目授权测试：执行测试会运行项目代码，执行器不是操作系统沙箱。发送前请检查服务地址和文件范围；敏感文件过滤及脱敏不能发现所有自定义秘密。

## 独立任务服务与历史

`mini-agent serve` 或 `python -m mini_agent.service` 启动本地 JSONL 服务；CLI 和已有 VS Code 面板共用 `runtime` 中的组件组装、任务执行与恢复逻辑。服务提供任务提交、状态查询、审批、取消、编辑器状态、持久化事件重放、历史及恢复，不依赖 VS Code。协议及示例见 [CORE_SERVICE.md](docs/CORE_SERVICE.md)。

可写任务及恢复在目标仓库的 `.mini-agent/core.lock` 上获取操作系统锁，覆盖取证、审批、写入及验证；同仓库不同进程或不同审计目录仍串行执行。进程退出后锁自动释放，不删除锁文件。只读任务无需仓库写锁。取消传递到模型、pytest、审批及仓库锁等待；客户端断开取消所属服务的活动任务。

`task.json` 记录任务状态，运行目录所有权用于区分仍运行与已中断任务。审批请求含唯一 ID 与提案内容版本，服务拒绝过期、重复或版本不匹配的回复。历史保留原测试结论，文件恢复状态单独记录；已中断任务可检查、预览并审批恢复，目前不能自动续跑。

```powershell
.\.venv\Scripts\python.exe -m mini_agent history --runs-dir D:\agent-runs
.\.venv\Scripts\python.exe -m mini_agent events --runs-dir D:\agent-runs --run-id <运行ID> --after 0 --limit 100
```

默认每次完整运行的 API 费用预估上限为 **¥0.5**，包含所有模型请求和重试。超出预估预算前停止发送；该限制基于配置单价和 token 估计，不能代替服务端账户额度或实际账单。

`verified` 只覆盖实际运行的测试。多文件修改不具有跨文件原子性，硬中断后请检查运行清单并执行恢复。
