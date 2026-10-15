# 本地任务服务

入口：`mini-agent serve` 或 `python -m mini_agent.service`。输入和输出均为 UTF-8 JSONL，每行一个对象。服务只使用本地 stdin/stdout；模型仍只能调用现有受限工具。

## 提交与响应

每个命令含 `version: 1` 和 `type`。可选 `request_id` 为最多 128 字符的字符串，用于匹配响应；响应类型为 `response`，含 `result` 或结构化 `error`。错误不回传任意异常内容。

提交参数与 CLI `run` 参数一致，任务文本是一项完整字符串，不解析为 shell 命令：

```json
{"version":1,"type":"start","request_id":"start-1","argv":["--project","D:\\your-project","--task","空输入应返回 0","--runs-dir","D:\\agent-runs","--read-only"]}
```

响应返回 `task_id`。服务异步输出携带该 `task_id` 的 `run_ready`、`audit`、`display`、`proposal`、审批及 `process_result` 事件。多个任务的事件可能交错，不能依赖提交响应先于任务事件；用 `task_id` 路由。

服务最多允许 16 个活动任务、保留 1000 个任务句柄；完成任务句柄可被回收，持久化历史仍可通过 `run_id` 查询。等待仓库所有权或准备期间，`run_id` 可以为空。

## 命令

| type | 字段 | 作用 |
| --- | --- | --- |
| `start` | `argv` 字符串数组 | 启动任务，默认 Mock；API/测试授权沿用 CLI 参数 |
| `status` | `task_id` | 查询服务内任务状态、退出码与待审批 ID/版本 |
| `approval` | `task_id`, `approval_id`, `proposal_version`, `approved` 布尔值 | 回复待审批请求 |
| `cancel` | `task_id` | 取消任务，包括锁等待、模型、测试和审批 |
| `editor_state` | `task_id`, `dirty_paths` 相对路径数组 | 更新未保存文件状态 |
| `run_status` | `runs_dir`, `run_id` | 查询持久化状态及可恢复性 |
| `history` | `runs_dir`, 可选 `limit` | 按时间倒序查询历史，默认 100，最多 1000 |
| `events` | `runs_dir`, `run_id`, 可选 `after`, `limit` | 重放审计事件，默认游标 0、最多返回 100 项 |
| `recover` | `argv`：`--runs-dir` 与 `--run-id` 参数 | 异步预览并审批文件恢复 |

输入行最多 64 KiB，参数最多 128 项。不支持 shell、自动安装或恢复推理上下文。

## 审批版本与重放

`approval_requested` 含 `kind`、完整 `payload`、唯一 `approval_id` 与 `proposal_version`。版本是提案 payload 的规范 JSON SHA-256；补丁包含原文、新文及原哈希，恢复 payload 还包含 manifest 哈希。审批请求及结果持久化在审计事件中。回复必须同时匹配任务、审批 ID、内容版本；重复回复不改变已选决定。旧面板协议继续接受仅含审批 ID 的回复，核心仍执行原有内容/哈希复检。

`events` 的 `seq` 是持久化日志行游标；`next` 用于下一次查询的 `after`。它与在线外层事件的每任务 `seq` 不同。重放审计记录不重新请求审批、发送模型请求或应用文件。进程中断造成的未完整末行不参与重放；随后追加时将末行残片保留为 `events-partial.txt`，继续写入完整事件。单份日志读取上限 64 MiB，单次最多返回 1000 项。

## 所有权、中断与恢复

可写任务及恢复持有目标仓库 `.mini-agent/core.lock` 的操作系统锁，覆盖整个任务。不同进程及不同审计目录都使用同一仓库锁；其他仓库可并行执行，只读任务无需仓库写锁。等待有超时和取消检查。锁文件永久保留，所有权由操作系统释放；不通过 PID 判断或删除文件抢锁。

`task.json` 记录开始、结束与最终结果，`owner.lock` 表示运行目录所有权。记录仍为 `running` 但所有权已释放时，查询返回 `interrupted`，保留原文件与审计证据。旧版本有 summary 的记录展示原状态；缺少所有权元数据且无 summary 时显示 `unknown`，不推断其仍在运行。

恢复沿用文件哈希、备份和未保存路径检查，审批后再次校验 manifest，并使用同一仓库锁。恢复后的状态见 `rollback.json` 和 manifest，历史测试结果不会被改写。只要有未恢复的写入条目，历史显示 `recovery_available`；是否能恢复仍由实际预检确定。

服务 EOF、输入损坏或退出时取消所有活动任务，最多等待总计 5 秒收尾。未正常收尾的任务在所属进程退出后可被识别为中断。运行期间的 OS DNS/连接建立仍受现有网络实现约束；不承诺任意阻塞系统调用都能瞬时终止。服务可重新启动并读取历史、重放事件及恢复文件；不支持自动续跑。
