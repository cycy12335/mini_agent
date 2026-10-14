# Mini Coding Agent

遵循 [PLAN.md](PLAN.md) 的 Python 命令行编程 Agent。当前完成阶段 0–4：只读工具、离线 Mock 循环、差异预览、审批、备份、应用、恢复与授权测试反馈。

`files` 可用于浏览目标项目，`run --model mock` 可跑通预设示例并在确认后应用补丁。显式授权测试后，先保存基线，再自动测试每次已批准的修改。`inspect` 查看记录，`rollback` 预览并确认恢复。Mock 是固定演示，不是通用模型；任务描述会记录在会话中，但不会改变预设修复策略。阶段 5 已加入 `--model api`、发送范围审批与 HTTP 适配器；真实模型验收状态见文末。

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

上述只读命令正常结束为 `proposed`，退出码 0；显示差异但不会请求写入或应用。去掉 `--read-only` 后，每次提案显示差异并询问 `应用此提案？[y/N]`；只有输入 `y` 才会应用，空输入、其他回答或输入结束均拒绝。未授权测试时批准应用后状态为 `unverified`、退出码 2，拒绝为 `cancelled`、退出码 3。`--allow-tests` 与 `--test-python` 必须同时提供，且不能和 `--read-only` 同用。`--model api` 需要 API 配置、本地密钥文件或环境变量凭据及发送范围确认。项目根通过完整路径权限检查，不会先消除符号链接再校验。

产物默认保存在 Agent 项目的 `.mini-agent/runs/<run-id>/`：`events.jsonl` 记录动作、工具结果、格式错误和结束事件；`proposals.json` 保存已接受的完整文件替换提案（没有提案时不生成）；`summary.json` 与 `report.md` 列出状态、已读文件、提案数及未验证限制。日志可能包含源码，默认被 Git 忽略。源码中的提示文本只作为数据，不改变工具权限。

可用 `--runs-dir <目录>` 指定审计根目录，必须在目标项目之外，不能经由符号链接或重解析点访问；该路径只由用户配置，模型不能指定。源码安装使用上述默认目录；安装到其他位置时可显式选择自己的可写产物目录。

模型每次只能返回一个动作：

```json
{"type": "tool_call", "name": "read_file", "arguments": {"path": "src/calculator.py"}}
```

结束动作是 `{"type":"final","message":"结论"}`。提案工具 `propose_edits` 接受 `edits` 数组，每项只有 `path`、`original_sha256` 和完整 UTF-8 `content`；仅允许已有 Python 源码/测试文件，禁止 `setup.py`、`conftest.py` 和配置文件。路径、大小、原始哈希和重复路径均须校验；模型只能提出提案，审批及应用由控制器完成。

非法 JSON、未知工具、缺参、多余字段或不合法类型最多允许一次纠正；再次无效结束为 `failed`。每次模型调用（包括无效动作和 final）占用一步，默认最多 20 步。支持 Ctrl+C 取消（`cancelled`，退出码 3）、步数/上下文/运行时间超限和执行错误（`failed`，退出码 1）。`proposed` 仅表示已有读取依据的未验证结论，是否生成提案应查看报告中的提案数。模型文字不决定测试状态：未执行时为 `not_run`；只有最新修改经过实际测试且基线比较满足要求，控制器才返回 `verified`、退出码 0。

时间预算在调度前和适配器返回后检查；Mock 是即时本地逻辑。HTTP 适配器为请求及其重试共享一个截止时间，连接后可中断慢响应；系统 DNS 解析仍受操作系统控制，返回后会再次检查预算，超时不发送正文。同步调用的外层检查不能强制中断任意自定义 Python 适配器。上下文超限时移除最旧的完整动作/结果组；任务和最新结果仍超限则明确停止，不静默丢弃最新结果。

配置使用 UTF-8 JSON，可只覆盖部分预算；未知字段、重复字段、非法类型与格式错误均被拒绝，预算要求正整数。文件、搜索、扫描、动作、提案大小、上下文和测试预算已生效。可选 `api` 对象配置非敏感的服务地址、模型、协议、凭据环境变量名及请求预算，服务配置文件不接受密钥值，密钥单独放在项目根目录的 `agent.credentials.json`。Mock 模式不读取模型凭据、不联网。

`.env.example` 为环境变量的占位说明，程序不会自动加载 `.env`。不要在配置或命令行参数中保存真实密钥。

## 只读工具（阶段 1）

```powershell
.\.venv\Scripts\python.exe -m mini_agent files --project .\examples\buggy_project list
.\.venv\Scripts\python.exe -m mini_agent files --project .\examples\buggy_project read src/calculator.py
.\.venv\Scripts\python.exe -m mini_agent files --project .\examples\buggy_project search average
```

列表与搜索支持 `--path src` 缩小到相对目录。所有工具输出统一 JSON：`ok`、`data`、`error_code`、`message`、`truncated`。工具成功返回 0，工具失败返回 1，CLI 参数/配置错误返回 2。`read` 返回内容、字节数和原始文件 SHA-256；搜索为区分大小写的字面量匹配，返回路径、行号及最多 500 字符的片段，长行标记 `line_truncated`。

工具只读取明确允许的 `.py/.pyi/.md/.rst/.txt/.toml/.json/.yaml/.yml/.ini/.cfg` 文件。列表返回符合路径规则的候选文件；读取和搜索额外检查 UTF-8、控制字符和大小。默认排除隐藏路径、依赖/构建目录、凭据/密钥相关文件名；拒绝路径穿越、绝对路径、Windows 盘符/UNC/设备名/备用数据流、符号链接、junction、其他重解析点和硬链接。目标目录不能覆盖 Agent 自身代码，示例子目录可用。

默认上限：读取 64 KiB、搜索 100 条、文件列表 1000 个、扫描 10000 个目录项、目录深度 32；列表路径和搜索片段文本合计上限为 32768 字符（不含 JSON 字段开销）。达到扫描或结果上限时标记 `truncated`。被路径规则过滤的条目以及搜索中无法读取的候选文件计入 `skipped`，因此无匹配不表示被跳过的文件也已检查。

`files` 子命令不存在写入、命令执行或模型调用入口。文件名过滤不能识别任意源码中内嵌的秘密；这些检查不能抵御恶意并发文件系统替换，也不是操作系统沙箱。API 模式另有发送范围与凭据特征检查，见下文。

## 应用与恢复（阶段 3）

先复制示例到新的演示目录（如果目录已存在，请换一个新名称），避免修改作为基线的示例：

```powershell
Copy-Item -Recurse .\examples\buggy_project .\examples\demo-stage3
.\.venv\Scripts\python.exe -m mini_agent run `
  --project .\examples\demo-stage3 `
  --task "average([]) 应返回 0" --model mock
```

审核差异后输入 `y`。未运行测试时退出码 2 是预期的“已修改但未验证”，不是测试失败。CLI 输出运行编号和报告路径，用实际编号替换以下 `<run-id>`：

```powershell
.\.venv\Scripts\python.exe -m mini_agent inspect --run-id <run-id>
.\.venv\Scripts\python.exe -m mini_agent rollback --run-id <run-id>
```

如果运行时指定了 `--runs-dir`，查看和恢复时也须传入同一目录。恢复先显示目标项目及文件清单，再次输入 `y` 才执行；后续人工改动导致哈希冲突时拒绝覆盖。`inspect` 显示原运行总结和当前事务清单；恢复后 `summary.json/report.md` 保留为历史报告，当前恢复状态见 `manifest.json`、`rollback.json` 和审计事件。

每次提案保留 `proposal-001.diff` 等独立统一差异，`proposal.diff` 指向最新预览内容。测试变更明确标记 `TEST CHANGE`；当前采取保守规则：保持已有测试 AST 不变，只允许追加不重名、无装饰器或默认参数的新 `test_*` 函数，禁止修改/删除已有断言或加入跳过标记来替代原测试。其他测试重构需要人工处理。

批准后重新检查路径、哈希及提案内容；变化使本次批准失效并结束运行，需重新读取、生成和审批提案。全部原始字节备份写入 `backups/`，并先持久化 `manifest.json`，然后用同目录临时文件逐个替换源码。清单包含每个文件的原/新哈希、权限及 `pending/writing/written/restored` 写入状态；原始 BOM 和换行保存在备份中。

多文件替换不是跨文件原子事务。失败或 Ctrl+C 时尝试恢复本次实际写入的文件；不会恢复尚未写入的文件，也不会覆盖已与 Agent 新内容不一致的人工修改。无法恢复的文件记录为冲突，保留备份供检查。硬中断或断电无法保证自动清理；可用 `inspect` 检查写前日志，再用 `rollback` 恢复。恢复先校验全部待恢复路径、哈希、备份以及批准对应的清单，再按事务逆序处理；中途失败可以重试，已经恢复的文件不会重复覆盖。

运行产物应保留到确认不再需要恢复为止。备份哈希检查用于发现损坏，不是防恶意修改本地日志的签名机制；不可信项目仍需更强的系统隔离。

## 授权测试（阶段 4）

仅对信任的项目开启测试：执行 pytest 就是在执行项目代码，不是安全沙箱。先创建示例副本和供该项目使用的虚拟环境，并自行安装 pytest；Agent 不安装依赖。

```powershell
Copy-Item -Recurse .\examples\buggy_project .\examples\demo-stage4
py -3 -m venv .\examples\demo-stage4\.venv
.\examples\demo-stage4\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m mini_agent run `
  --project .\examples\demo-stage4 `
  --task "average([]) 应返回 0" --model mock `
  --allow-tests `
  --test-python .\examples\demo-stage4\.venv\Scripts\python.exe
```

预期：基线 1 failed、1 passed；显示差异，输入 `y`；修复后 2 passed，状态 `verified`。恢复方式同上一节。解释器也可以位于项目外，但必须是用户指定的已有虚拟环境 Python；相对路径以启动目录为准，POSIX 虚拟环境的解释器符号链接保留原路径。

控制器固定使用参数列表 `<python> -m pytest tests -q`、`shell=False`、目标项目为工作目录、标准输入关闭。模型只能请求 `{"type":"tool_call","name":"run_tests","arguments":{}}`，不能选择命令、解释器或 pytest 参数。测试执行默认关闭，只读模式不会启动测试进程。

先运行基线，普通测试断言失败可继续修复；收集错误、缺少 pytest、不完整记录、超时或清理失败会停止。每个已批准的补丁自动测试，失败结果返回模型，后续提案仍逐次审批。Mock 只提出一次预设补丁，不会自主诊断其他问题。全部跳过、无测试、缺失或新增跳过的基线用例都不能用来认定修复成功。报告区分原有失败、已修复失败、剩余失败、新增失败和缺失用例；`verified` 只覆盖实际执行的测试范围。

默认单次测试 60 秒，受整次运行剩余时间进一步限制；到期或取消后终止进程树并收尾。持续排空 stdout/stderr，仅保留前 32 KiB，截断显式标记；UTF-8 不完整或无效字节丢弃。pytest 结构化记录最多 1000 个用例、每个 ID 512 字符、总 ID 100000 字符，JSON 上限 256 KiB；超限标记不完整，不能判为通过。测试输出与项目文件同样是数据，不具有授权作用。

Windows 在挂起状态创建解释器，先加入禁用 breakaway、关闭即终止的 Job Object，再恢复主线程；Linux 使用独立 session/process group。正常结束也清理遗留后代；测试覆盖子进程和孙进程的超时、取消及正常结束。进程清理另有短暂收尾时间。Linux 中主动 `setsid` 脱离进程组、通过系统服务另起进程等不在此可信项目执行器的隔离保证内。环境采用允许列表，移除模型/云凭据及继承的 Python/pytest 注入参数，并禁用第三方插件自动发现；项目显式配置及 `conftest.py` 仍会执行，也仍能访问用户权限允许的文件和网络。需要隔离的不可信项目应使用外部沙箱。

每次测试保存 `test-001.json`（命令、退出码、超时、取消、截断、清理及用例）、`test-001-output.txt` 和可用时的 `test-001-results.json`。`test-output.txt` 为最新输出；基线和后续结果都保留，`summary.json/report.md` 包含比较与恢复入口。未授权时不会生成测试产物。

实现参考 Python [subprocess 文档](https://docs.python.org/3/library/subprocess.html) 和 Microsoft [Job Objects 文档](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

## 真实模型（阶段 5）

本项目选用 **DeepSeek V4.1 Flash**，官方 API 模型 ID 为 `deepseek-flash`；使用 `https://api.deepseek.com/responses` 的 Responses 协议。[模型官方说明](https://api-docs.deepseek.com/news/news260910/)、[DeepSeek Responses 接口](https://api-docs.deepseek.com/api/create-response/)。

`agent.api.example.json` 已配置该服务、`reasoning_effort: low`、8192 输出 token、1 MiB 响应上限和最多 2 次临时错误重试。模型和地址均可配置。预算包括推理 token，但不保存或发送模型返回的隐藏推理。API 会产生服务费用；token 用量只依据响应提供的数据累计，缺失部分明确标记未知，不做精确费用承诺。

直接编辑项目根目录的 **`agent.credentials.json`**，把 `api_key` 的空字符串换成自己的密钥，保存后照常运行 API 模式：

```json
{
  "api_key": "在这里填写你的 API Key"
}
```

程序每次启动会重新读取这个文件，不需要设置 PowerShell 环境变量，也不需要重启终端。文件已加入 `.gitignore`，不会作为项目文件发送给模型；密钥仍参与日志及输出脱敏。密钥以明文保存在本机，请勿提交或分享该文件。服务地址、模型与请求预算仍放在 `agent.api.example.json`。

读取优先级：文件中的非空 `api_key` 优先；文件不存在或密钥为空时，回退读取 `api.api_key_env` 指定的环境变量（默认 `MINI_AGENT_API_KEY`）。`api_key_env` 设为空字符串可禁用环境变量回退。密钥文件只允许一个字符串字段 `api_key`，格式错误会直接报错而不会联网。`api_key` 必须填写平台生成的实际密钥，不能填变量名 `MINI_AGENT_API_KEY`；这种误填也会在联网前拒绝。`.env` 和 `.env.example` 仍不会自动加载。

复制全新示例，先手动准备测试虚拟环境（Agent 不安装依赖）：

```powershell
Copy-Item -Recurse .\examples\buggy_project .\examples\demo-stage5
py -3 -m venv .\examples\demo-stage5\.venv
.\examples\demo-stage5\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m mini_agent run `
  --project .\examples\demo-stage5 `
  --task "修复 average([]) 崩溃，空列表返回 0，保留非空输入行为" `
  --model api --config .\agent.api.example.json `
  --send-path src --send-path tests `
  --allow-tests --test-python .\examples\demo-stage5\.venv\Scripts\python.exe
```

先审核接收服务、模型和文件清单，输入 `y` 同意发送；然后在每个补丁的差异展示后单独确认。拒绝发送会在任何模型请求或项目测试之前结束。`--read-only` 仍需发送授权，但不会执行测试或写入源码。没有密钥会直接报配置错误，不发起网络请求。

`--send-path` 可重复指定项目内相对文件或目录；默认列出全部符合文件策略的候选文件供审批，不自动上传全部内容。首次请求含任务、工具说明和已批准文件名；内容由模型通过受控工具按需读取。范围在运行开始时固定，新增或范围外文件不能被读取、搜索返回或修改；扩大范围需要重新启动并确认。库存超限必须缩小范围，不能批准截断的不完整清单。

发送授权还涵盖工具结果、补丁结果和已授权测试输出；测试代码可能自行输出其他内容，因此文件清单不是对任意测试输出来源的隔离保证。已知 API 凭据、常见密钥格式与凭据赋值文本会脱敏；含这些特征的源文件直接禁止自动发送和修改，避免脱敏文本被当作真实源码写回。规则可能误报，也无法识别所有自定义秘密，发送前应人工确认范围。审计不记录 Authorization、原始 HTTP 错误正文、完整模型响应或隐藏推理；日志与报告保留可验证动作及用量。

适配器采用标准库 HTTP/TLS，验证证书；不跟随重定向，不继承代理环境、Cookie 或认证处理器。远程地址必须为 HTTPS，拒绝 URL 凭据、查询参数和片段；仅回环地址允许 HTTP，用于本地集成测试。认证失败不重试；限流、部分 5xx/408 和临时连接故障最多重试 2 次，且共用单次模型及整次运行剩余预算。输出字节超限、截断、拒绝和异常响应会明确失败；动作结构仍执行原有严格校验，最多一次格式纠正。

内部工具协议不直接授予服务端工具能力：请求要求返回一个 JSON `tool_call` 或 `final` 对象，所有动作在本地验证。使用 JSON 模式而非假定服务一定遵循动作 schema；不支持的字段、路径或任意 shell 请求均拒绝。可配置的 `chat_completions` 分支使用 OpenAI 的 `response_format` / `max_completion_tokens`；其他兼容服务需逐项核对支持情况，本项目 DeepSeek 配置使用 Responses 分支。[DeepSeek 兼容性说明](https://api-docs.deepseek.com/guides/responses_api/)、[OpenAI JSON 模式说明](https://developers.openai.com/api/docs/guides/structured-outputs)。

使用环境变量的用户仍可用 `Remove-Item Env:MINI_AGENT_API_KEY` 清除当前终端变量。使用文件的用户可清空 `agent.credentials.json` 的 `api_key`；不会自动删除 Windows 用户环境变量。

## CI

[GitHub Actions 工作流](.github/workflows/ci.yml) 在 push、pull request 或手动触发时运行，覆盖 Windows/Linux 与 Python 3.11、3.12、3.13、3.14。每个组合执行可编辑安装、CLI 检查、Agent 测试及示例基线检查，工作流只有仓库读取权限。

`python scripts/check_example_baseline.py` 验证示例准确产生一个除零失败和一个正常通过；其他失败、测试被跳过、测试集变化或缺陷意外消失均使检查失败。它是开发/CI 脚本，不是提供给模型的测试工具。

工作流使用方式参考官方 [checkout](https://github.com/actions/checkout) 与 [setup-python](https://github.com/actions/setup-python) 文档。

## 验收与后续开发

2026-10-13 阶段 0 验收记录：全新 `.venv` 可编辑安装成功；模块 `--help` 退出码 0；Agent 测试 18 passed；示例基线 1 failed、1 passed，失败原因为 `average([])` 的 `ZeroDivisionError`；携带示例配置的 `run` 按设计报告未实现，退出码 1。

2026-10-13 阶段 1 本地验收：73 passed、2 skipped。Windows junction 和硬链接检查通过；本机无符号链接创建权限，因此该测试跳过；POSIX FIFO 测试仅在 Linux 运行。示例基线检查通过，保留预期的空列表除零错误。跨平台结果以 GitHub Actions 实际运行为准。

2026-10-13 阶段 2 本地验收：110 passed、1 skipped（Windows 不执行 POSIX FIFO）；真实符号链接与 junction 检查通过。实际 CLI 以 6 次动作完成 Mock 提案流程，返回 `proposed`；示例基线仍为准确的 1 failed、1 passed。提案与日志保存在本地运行目录，目标源码未应用修改。

2026-10-13 阶段 3 本地验收：140 passed、1 skipped（Windows 不执行 POSIX FIFO）。示例副本通过真实 CLI 确认应用，Agent 正确返回 `unverified`；开发者单独运行副本测试得到 2 passed，随后通过 CLI 恢复，源码 SHA-256 与原始内容一致。该开发验证不代表 Agent 已实现测试执行器。

阶段 4 验收包括真实 CLI 的基线失败 → 审批应用 → 测试通过、哈希一致恢复、未授权不启动进程、敏感环境清理、输出洪泛截断、缺失依赖、非零退出、进程树清理及测试失败后再次审批修复。现有阶段的单元测试和集成测试均由跨平台 CI 自动执行。

阶段 5 代码已实现，离线 HTTP 集成覆盖协议转换、授权拒绝、范围锁定、脱敏、认证失败、限流重试、慢响应超时/取消、格式异常及端到端修复。脚本服务与 Mock 通过不能替代真实模型验收；真实 DeepSeek 修复案例尚待执行并记录，完成后再进入阶段 6。
