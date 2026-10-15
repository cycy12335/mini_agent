# V2 候选集验收

日期：2026-10-15。Core-1 至 Core-6 后新增候选集与评估器，版本 0.7.0。候选集未用于此前核心实现，但由本轮开发新编写，规模较小，不能替代第三方仓库盲测或推广为通用修复成功率。

## 固定候选集

| 案例 | 行为问题 | 主要验证 |
| --- | --- | --- |
| batch_total | 批次末条记录未参与求和 | 跨文件追踪、边界输入、负数和小数 |
| interval_overlap | 半开区间交集长度错误 | 无既有用例、创建回归、修复前失败证明 |
| dynamic_prefix | 动态分发后的前缀移除错误 | 动态调用的不确定性、空前缀和重复前缀 |
| unavailable_dependency | 缺少明确不存在的依赖 | 基线阻塞、不安装依赖、不调用模型 |

任务文本只描述 Bug，不指定文件。每个仓库副本拥有可自动探测的 `.venv`，复用指定可信解释器已有的 pytest，不安装新依赖。发送候选为 20 个合成文件，原始仓库包含源码、公开测试和 pytest 配置。隐藏测试、故障位置标签和评分元数据均在仓库之外，模型的文件工具无法读取。

`suite.json` 冻结任务、初始文件哈希、隐藏测试哈希与核心模块指纹。核心变化、隐藏测试变化或额外答案文件都会使该候选集不能直接用于同一次真实验收。每次真实尝试写入 `live-session.json`，不覆盖失败记录；再次尝试需新建候选集，重新记录授权，避免选择性保留最佳结果。

## 操作

```powershell
# 每次选择一个尚不存在的目录
.\.venv\Scripts\python.exe -m mini_agent.acceptance prepare --destination .pytest-tmp\my-acceptance --test-python .venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m mini_agent.acceptance offline --suite .pytest-tmp\my-acceptance
.\.venv\Scripts\python.exe -m mini_agent.acceptance plan --suite .pytest-tmp\my-acceptance --config agent.api.example.json
.\.venv\Scripts\python.exe -m mini_agent.acceptance live --suite .pytest-tmp\my-acceptance --config agent.api.example.json
.\.venv\Scripts\python.exe -m mini_agent.acceptance report --suite .pytest-tmp\my-acceptance
```

`live` 默认逐项显示发送范围与补丁并询问审批。`--consent <JSON>` 只用于已经取得明确授权的整批合成副本验收，记录必须匹配 `suite_id`、`frozen_sha256`、配置文件 SHA-256，并包含 `approved_by` 以及 `allow_disclosure/allow_tests/allow_guarded_patches=true`。凭据存在不会自动授予此权限。该模式仍拒绝既有测试预期调整；任何新文件与补丁继续受核心路径、断言、证据和哈希检查约束。批次授权计为一次人工介入，自动审批次数单独记录。

示例 API 配置每个任务的本地估算上限为 ¥0.5，4 个候选任务整批上限为 ¥2。这个数值来自已有配置和客户端成本预留，不是对服务商价格或账单的独立认证。隐藏测试评估和离线探针没有模型调用费用。

## 评分方法

隐藏测试先在独立评估副本上证明旧代码失败；真实模型运行只使用原项目里的公开测试。运行结束后在另一个评估副本中加入隐藏测试，并执行公开、新增和隐藏测试。隐藏测试不会写入模型项目，也不会进入后续模型上下文。

修复成功要求：原隐藏测试确实失败、核心报告 `verified`、修复后的独立评估完整通过、无新增失败/缺失/跳过回归，并且原公开 Python 测试未被修改。评估异常保存为失败记录，不能直接采用核心的成功状态。

`evidence_location_match` 只衡量根因引用的修复前证据是否覆盖标注位置。根因语义正确性默认 `null`，需人工复核后才统计 `diagnosis_accuracy`。可用 `report --reviews reviews.json` 提供复核结论：

```json
[
  {"case_id":"batch_total","run_id":"实际运行ID","diagnosis_correct":true,"reviewer":"复核者","note":"核对触发条件、因果解释与证据的具体理由"}
]
```

复核必须对应唯一的真实运行；原始运行结果保持不变。报告包含实际请求数、修复结果、测试回归、耗时、配置费用估算、token 使用和人工介入。未调用模型的尝试与离线探针不进入真实模型修复成功率分母；缺依赖作为阻塞能力单列。

## 本轮记录

离线候选集目录：`.pytest-tmp/v2-acceptance-20261015-01`。发送清单：该目录下 `execution-plan.json`。真实重跑目录：`.pytest-tmp/v2-acceptance-20261015-02`，任务、初始文件内容、隐藏测试和 API 配置与原授权清单一致，授权派生记录见 `consent.json`。

离线验证：3 个隐藏测试基线均确认旧缺陷失败；8 项可靠性探针全部通过，涵盖缺依赖、用户修改保留、取消、模型失败、费用预算、动作预算、时间预算及硬中断后恢复。探针使用受控适配器或故障注入，不演示自动修复，不计入真实模型成功率。原始候选仓库保持不变。

首次授权尝试因 Windows 重定向输出的编码无法编码 `¥` 字符，在 API 请求前全部停止。记录保留在 01 目录，实际请求数 0、费用预留 0。验收入口已固定为 UTF-8，并新增回归测试；没有覆盖原失败记录。

用户明确授权后，以同样的配置、任务和文件内容在 02 目录重跑，结果如下：

| 案例 | 结果 | 实际请求数 |
| --- | --- | --- |
| batch_total | `failed / model_timeout`，60 秒响应预算耗尽 | 1 |
| interval_overlap | `failed / model_timeout`，60 秒响应预算耗尽 | 1 |
| dynamic_prefix | `failed / model_timeout`，60 秒响应预算耗尽 | 1 |
| unavailable_dependency | `failed / test_dependency_unavailable`，正确阻塞 | 0 |

核心任务累计约 184.4 秒，整批人工授权 1 次；没有有效模型动作或修复提案，原候选文件保持不变。本次端到端修复完成 0/3；因全部模型请求超时，不能据此判断模型实际定位与修复能力，根因语义正确率保持未评估。超时请求没有服务端 token 用量，客户端保守计费预留合计 **¥0.124392**，实际服务商账单未知；`cost_known` 仅表示有客户端估算，不表示账单已知。

连接诊断不发送凭据或模型载荷，TLS 连接约 0.113 秒，未认证 HEAD 返回 401，见 02 目录的 `connectivity.json`。这仅证明该次连接可达，不表示真实凭据或模型请求正常。[DeepSeek 官方接口说明](https://api-docs.deepseek.com/api/create-response/)列明 `/responses` 与 `deepseek-flash`；当前证据不足以确定超时发生的上游原因，未擅自改动服务、协议或超时预算。

原始结果保存在 02 目录的 `live-results.json`、`live-session.json` 和 `runs/` 中；离线探针及两个真实尝试都保留供复查。下一步应先排查带认证模型请求的响应与超时，再重新冻结候选集复验。真实模型验收尚未通过。

工具验证：完整源码 **415 passed、1 skipped**；VS Code Node 测试 **16 passed**。0.7.0 wheel 已离线构建并重新安装，安装版候选集/8 项可靠性探针/3 项隐藏基线检查，以及独立服务、修复、CLI、诊断和重启恢复检查均通过。

后续 API 专项排查已定位到收到 HTTP 200 后等待上游响应体：最小请求、关闭推理、Chat 对照与原生 curl 均复现；模型列表认证可用。具体上游排队/调度原因尚未得到服务端确认，见 [API_TIMEOUT_DIAGNOSIS.md](API_TIMEOUT_DIAGNOSIS.md)。没有据此修改原验收结果。

## 完整验收补充（2026-10-15）

用户本轮明确反馈：“我自行验证真实API通过，记录通过”。真实 API 连通与调用验收据此记录为**用户自验通过**；本轮自动验收没有重复请求真实 API，没有新增调用费用。此前 01/02 候选集的失败与超时证据保留，不改写既有评分；用户自验未提供这四个冻结案例的逐案运行记录，因此不据此生成新的候选集修复成功率或根因准确率。

本轮源码、安装版、插件及打包检查见 [完整验收报告](FULL_ACCEPTANCE_2026-10-15.md)。
