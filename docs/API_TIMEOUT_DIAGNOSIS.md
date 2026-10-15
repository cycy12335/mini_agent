# API 响应超时排查

日期：2026-10-15。目标：`https://api.deepseek.com/responses`，模型 `deepseek-flash`；沿用 `agent.api.example.json`。

## 结论

已定位到等待响应体阶段：本次请求很快完成 TLS、发送并收到 HTTP 200，但上游在客户端截止时间内没有返回模型结果，因此触发 `MODEL_TIMEOUT`。

带认证的模型列表返回 200，且包含配置模型；最小请求、关闭推理、Chat 接口和独立的原生 curl 客户端都能复现等待正文。这些证据排除了普通认证拒绝、原任务/工具清单过大或 Python HTTP 读取实现作为共同主因。不能仅凭 HTTP 200 判断推理已经开始或请求最终成功。

更可能是上游模型服务尚未进入生成或结果迟迟未交付，可能涉及排队/调度。具体的上游内部原因仍需服务方日志确认，客户端不能证明某一种内部故障。未擅自增大生产超时、切换模型或关闭 TLS 校验。

## 实测证据

| 对照 | 结果 |
| --- | --- |
| 原验收首轮请求重放，11745 字节 | TLS 约 0.12 秒，发送完成约 0.14 秒，HTTP 200 响应头约 0.249 秒；此后响应体 0 字节，约 60 秒截止后终止 |
| 带认证 GET `/models` | HTTP 200，`deepseek-flash` 存在，约 0.26–1.27 秒 |
| 282 字节最小 Responses、`reasoning=none` | HTTP 200，30 秒内正文 0 字节 |
| 281 字节最小 Responses、`reasoning=low` | HTTP 200，30 秒内正文 0 字节 |
| 302 字节最小 Chat、关闭推理 | HTTP 200，30 秒内正文 0 字节 |
| 流式 Responses、关闭推理 | HTTP 200，约每 12 秒收到 14 字节短块，累计 28 字节，没有 `response.*` 模型事件；30 秒目标窗口后收尾约 36.3 秒 |
| 原生 curl / Schannel，HTTP/1.1，绕过代理 | TLS 约 0.117 秒，首响应头约 0.350 秒；20 秒下载 0 字节，curl 退出码 28 |

流式短块与保活行为一致；诊断只记录长度和事件类型，没有保存正文，故不把短块的具体文本作为已证实事实。[DeepSeek 官方说明](https://api-docs.deepseek.com/quick_start/rate_limit/)明确：等待期间可返回非流式空行或 SSE 保活注释，若请求 10 分钟内未开始推理，服务器会关闭连接。这与“HTTP 200 已收到，但仍未获得生成结果”的观察一致，不能据此确认某个账户或模型后端的确切排队状态。

[官方 Responses 参数说明](https://api-docs.deepseek.com/api/create-response/)支持 `deepseek-flash` 及 `reasoning.effort=none/low`；[官方模型列表接口](https://api-docs.deepseek.com/api/list-models/)用于检查模型可用元数据。模型列表可访问并不保证单次推理会按时完成。

## 复查入口

脚本：[scripts/diagnose_api_timeout.py](../scripts/diagnose_api_timeout.py)。从工作区使用已授权的可信配置执行：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
# 原合成案例的首次请求，仅一轮动作，不应用补丁
.\.venv\Scripts\python.exe scripts/diagnose_api_timeout.py
# 不含仓库数据的最小请求，none/low 对照
.\.venv\Scripts\python.exe scripts/diagnose_api_timeout.py --controls
# 流式 Responses 与非流式 Chat 对照
.\.venv\Scripts\python.exe scripts/diagnose_api_timeout.py --protocol-controls
# 原生 curl 独立客户端对照
.\.venv\Scripts\python.exe scripts/diagnose_api_timeout.py --native-control
```

脚本只记录阶段、耗时、状态、字节数、事件类型及客户端费用预留。密钥不写入 argv、文件或日志；curl 密钥通过 stdin 配置传入，响应正文丢弃。诊断不发送隐藏测试，不修改候选源文件。原案例沿用 ¥0.5 单例上限，最小对照每组最多 ¥0.1、原生对照最多 ¥0.05；这些是本地配置费用估算约束。

原始证据（工作区内保留）：

- 重放：`.mini-agent/api-diagnostics/3537ce1fcfd4/20261015T035454Z-37f9264dc653/events.jsonl`，事件 `http_trace`。
- 最小请求：`.mini-agent/api-diagnostics/controls-cc261adfdd4d/report.json`。
- 协议对照：`.mini-agent/api-diagnostics/controls-e18fa97640ee/report.json`。
- 独立客户端：`.mini-agent/api-diagnostics/native-87801c5570f7/report.json`。

本轮 6 次推理请求均没有完整用量回报，客户端保守费用预留合计 **¥0.060106**；连同之前真实验收的 ¥0.124392，累计 **¥0.184498**，仍低于已授权 ¥2。实际服务商账单未知。模型列表请求不属于推理请求。

## 后续处理

先检查服务方当时的模型可用性、账户调度和排队记录，恢复后以最小无仓库数据请求确认能获得完整 JSON，再重新冻结候选集进行真实验收。当前证据不支持用反复重试、切换协议或单纯放宽超时来宣称问题已解决。核心 60 秒默认预算、取消与费用约束保持原样。

## 后续用户复验（2026-10-15）

用户反馈真实 API 已自行验证通过。本节记录用户复验结论；上述超时日志保留为历史诊断，不代表当前 API 仍不可用。本轮没有重复发起真实 API 请求，未提供服务端层面的历史超时原因确认。
