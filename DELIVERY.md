# V1 交付与阶段 6 验收

日期：2026-10-14（Asia/Shanghai）。版本：0.1.0。开发环境：Windows / Python 3.14.6 / pytest 9.1.1。

## 实际案例

| 案例 | 运行方式 | 原始结果 | 修复/结束结果 |
| --- | --- | --- | --- |
| 空列表平均值 | 真实 DeepSeek V4.1 Flash，自主读文件和提案 | 1 failed、1 passed | 审批应用后 2 passed，`verified`；CLI 恢复后源码哈希一致 |
| 空白字符串解析 | 脚本适配器调用实际工具、审批、写入和 pytest | 1 failed、2 passed | 空白字符串返回 `[]`，保留数字解析及非法值拒绝；3 passed，恢复字节一致 |
| 缺少依赖 | 实际 pytest 基线和控制器 | ModuleNotFoundError，收集退出码 2 | `failed/test_dependency_unavailable`，0 个模型动作、无源码修改；提示检查选定虚拟环境的依赖和导入路径 |

字符串与缺少依赖夹具在 `examples/string_input_project`、`examples/missing_dependency_project`，与空列表夹具一样故意保留缺陷。只在副本中做修复演示。缺少依赖夹具使用虚构模块，目的是验证停止和提示，不应尝试从包索引安装它。

字符串案例使用测试脚本模型验证控制器，不宣称真实模型已完成字符串案例。真实模型验收由第一行独立满足。普通 pytest 仅收集 Agent 的 `tests/`，原始示例失败不混入 Agent 测试结果。

## 真实模型证据

- 服务：`https://api.deepseek.com/responses`；请求和返回模型：`deepseek-flash`。
- 运行编号：`20261014T010802Z-ea7093b9fa73`，基于阶段 5/预算实现 `19aea20`。
- 合法动作：读取 `src/calculator.py`、读取 `tests/test_calculator.py`、提出补丁、结束；包含一次格式纠正，共 5 次模型请求。
- 仅修改 `src/calculator.py`，加入空列表守卫及相应说明，原有测试没有改动。
- 输入 7386 token、输出 492 token，5 次请求都有用量记录，没有未知请求。
- 按配置的高峰、缓存未命中单价计算，费用上界估计为 **¥0.018708**，预算 ¥0.5。不是已核对的平台账单金额。
- 源码恢复成功，日志和备份扫描未发现本次使用的 API 密钥。

本地完整证据：`.mini-agent/stage6-verification/live/20261014T010802Z-ea7093b9fa73/`，包含动作、审批、差异、测试、费用预留、报告及恢复记录。恢复后原 summary/report 保留历史 `verified`，当前恢复状态见 manifest/rollback。

## 安装与回归证据

全新环境 `.mini-agent/stage6-verification/clean-env/` 未开启 system-site-packages；通过 `pip install ".[dev]"` 构建并安装 wheel。模块帮助与 `scripts/check_installed_cli.py` 通过，后者执行实际已安装 CLI 的只读提案、批准修改、基线/修复后测试、恢复及缺少依赖停止，无模型 API 调用。

干净环境全量结果：**241 passed、1 skipped**；唯一跳过为 Windows 不适用的 POSIX FIFO。JUnit：`.mini-agent/stage6-verification/unit-tests.xml`。安装 CLI 初次通过会话：`.mini-agent/installed-cli/8bb26251ae84/`，后续重验证会话同样保存在 installed-cli 下。

CI 在 Windows/Linux × Python 3.11、3.12、3.13、3.14 上执行非 editable wheel 安装、全部测试、故意失败的原始空列表基线及已安装 CLI 恢复检查。真实 API 不在 CI 中调用。

本阶段修正了安装相关问题：凭据文件以启动目录解析，wheel 安装不依赖源码目录；pytest 只注入独立的标准库 reporter，不将 Agent 的 site-packages 加入目标解释器，缺少 pytest 时不能借用 Agent 的 pytest；自保护同时识别运行中的包和含 Agent 源码特征的 checkout，允许 checkout 下独立的示例子项目。

## V1 验收清单与证据位置

| PLAN.md 条件 | 证据 |
| --- | --- |
| 指定项目和任务即可开始、无需粘贴源码 | 真实模型案例；CLI run 参数 |
| 实际读取及修改文件有记录 | live events/summary/manifest |
| 每次写入先展示差异并确认；只读不写 | test_stage3_cli.py；安装 CLI 检查 |
| 基线失败 → 修改 → 测试通过 | live baseline/post_edit；test_release_cases.py |
| 至少一个真实模型案例 | DeepSeek 运行编号及用量，如上 |
| 命令、退出码、超时、限制和恢复有报告 | test_test_runner.py；live report/summary |
| 非法动作与缺失文件不会失控循环 | test_actions.py、test_controller.py、test_http_api.py |
| 网络、认证、预算及取消明确收尾 | test_http_api.py、test_costs.py、test_controller.py |
| 写入失败恢复、人工修改冲突保护 | test_patching.py、test_stage3_cli.py |
| 单元与临时目录集成覆盖安全和恢复 | 上述测试及 test_release_cases.py |
| 路径、敏感文件与链接拒绝 | test_policy.py、test_files.py |
| 模型不能执行 shell、安装依赖或 Git | 严格动作 schema；test_actions.py；缺少依赖案例 |
| 首次发送审批、范围固定、凭据脱敏 | test_http_api.py；live disclosure_approval 与泄漏扫描 |
| 未授权测试不启动进程、固定测试入口 | test_test_runner.py；test_controller.py |

## 交付边界

- 面向用户信任的小型 Python 项目；文件、输出和上下文均有上限。
- 测试执行不是 OS 沙箱。项目可以执行其代码及显式插件、访问用户权限允许的文件/网络。Linux 主动脱离进程组及另起系统服务不在隔离保证内。
- V1 仅替换已有 Python 文件，测试修改保守地只允许追加新测试，不自动创建文件、调整依赖或 Git 状态。Mock 仅处理预设平均值示例。
- 文件过滤及脱敏不能发现所有自定义秘密，不能抵御恶意文件系统竞争。真实发送前仍须审核接收服务和范围。
- ¥0.5 为每次完整运行的客户端预估拦截，包含所有请求和重试。价格变化、服务忽略限制、其他客户端用同一密钥均不由本项目的预算控制。
- DNS 解析受操作系统控制；连接后检查截止时间并可中断慢响应。硬中断/断电后须检查清单并恢复。
- `verified` 只涵盖实际执行的测试；未执行、全部跳过、测试消失、未修复失败或依赖不完整不能判为修复成功。

具体安装、发送审批、参数、配置与恢复操作见 [README.md](README.md)。后续范围按 PLAN.md 的 V2 路线推进。
