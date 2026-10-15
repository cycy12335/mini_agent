# 面板 stdio 协议 v1

通过本地 stdin/stdout 与 Mini Agent 交互，每行一个 UTF-8 JSON 对象。

## 启动

```text
python -u -m mini_agent.panel --project <absolute-path> --task <description> --model mock --runs-dir <external-path> [--read-only]
```

其余运行参数与 `mini-agent run` 相同。原有 CLI 不受影响。
恢复入口：

```text
python -u -m mini_agent.panel rollback --runs-dir <external-path> --run-id <run-id>
```

该入口打开既有运行，验证备份与文件哈希后请求 rollback 审批，不会自动恢复。无需模型配置或密钥。
stdout 为 UTF-8 JSONL，stderr 是启动诊断。客户端保持 stdin 打开；断开输入时核心收到取消请求。
一个进程对应一个运行，因此审批只能作用于该进程内当前等待的提案。

## 事件信封

`{"version":1,"seq":1,"run_id":"...","time":"<UTC ISO timestamp>","type":"run_ready",...}`

seq 在进程内单调递增；初始化之前 run_id 可以为 null。

| type | 字段 | 含义 |
| --- | --- | --- |
| run_ready | directory | 运行编号已生成，产物目录由核心决定 |
| audit | entry | 已持久化的原有审计事件 |
| display | text | 简短执行输出，不包含隐藏推理 |
| proposal | payload | 原始/修改快照及统一 Diff，只读模式也产生 |
| approval_requested | approval_id, kind, payload | 等待 disclosure、proposal 或 rollback 审批 |
| recovery_result | status, files 或 error_code/message | 恢复完成、取消、无须恢复或失败；不改写原始 summary/report |
| approval_resolved | approval_id | 审批结束或被取消/超时 |
| command_error | message | 版本不匹配、无效或过期回复；不回显输入 |
| process_result | exit_code | CLI 已完成，最终结果以 summary/manifest 为准 |

disclosure 包含服务地址、模型、协议、费用预算、允许发送文件与数据范围说明。
proposal 包含 diff 和 files；每个文件含 path/original/replacement/original_sha256。提案快照不可修改，审批 ID 唯一；核心应用前重验内容哈希。
事件在输出前脱敏；凭据不进入面板。完整快照只供扩展宿主原生 Diff，Webview 只需要统一 Diff 和文件名。

## 客户端命令

```json
{"version":1,"type":"approval","approval_id":"<current-id>","approved":true}
{"version":1,"type":"cancel"}
```

approved 必须是布尔值；错误 ID、重复审批和历史审批拒绝执行。
输入每行最多 16 KiB；过长/截断输入按断开处理，取消运行。命令读取线程独立于执行，等待审批不阻塞取消。
审批等待计入运行预算；没有回复不会自动批准。取消与断开可终止审批等待、模型请求和测试；文件写入仍遵循已有恢复清单机制。

## 状态与生命周期

核心结果状态保持 verified/proposed/unverified/failed/cancelled；等待审批是 UI 阶段，不替换结果语义。
关闭面板不结束进程；同一扩展宿主重开面板可重显审批。扩展卸载/窗口关闭则取消任务，不支持继续执行。
审计文件支持历史重读；审批和原生 Diff 快照目前是宿主内存状态，重启后不恢复审批，历史查看统一补丁。
rollback 审批 payload 包含 project/files/notice。预览失败时不请求审批；审批后重新验证备份、文件哈希和清单版本。恢复审计事件追加到原运行，原 summary/report 保持历史结果。
恢复状态包括 rolled_back/cancelled/nothing_to_restore/failed。客户端读取 manifest 判断清单状态，不把历史 verified 当作恢复后的验证结果。
面板连接管理单次任务，多任务集成可使用 [本地任务服务](CORE_SERVICE.md)；不支持自动续跑。
