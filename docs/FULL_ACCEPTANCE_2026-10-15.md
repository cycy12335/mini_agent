# mini_agent 完整交付验收（2026-10-15）

本轮自动验收通过，真实 API 按用户自行验证的结果记录为通过。验收范围为 Python 核心 0.7.0、VS Code 侧边栏插件 0.6.0、离线候选集、安装包与 Git 交付。真实 VS Code GUI 的主题、布局及系统 SecretStorage 操作未在本轮人工执行，不据此宣称这些界面项目已完成视觉验收。

## 验证结果

| 检查 | 结果与证据 |
| --- | --- |
| Python 源码完整回归 | `415 passed, 1 skipped in 127.59s`；Windows 上跳过 POSIX FIFO 测试 |
| VS Code 插件 | `npm test`：22 passed；`npm run check`：全部 JavaScript 语法检查通过 |
| wheel 构建与安装 | 离线构建 0.7.0 wheel 和 sdist，使用 `--no-index --force-reinstall --no-cache-dir` 安装 |
| 源码一致性 | 37 个 Python 模块在源码、wheel 和实际安装目录逐字节一致 |
| 源码包内容 | 包含文档、示例及验收脚本，不包含本机凭据、审计目录和演示副本 |
| 示例缺陷基线 | `check_example_baseline.py` 正确确认 1 个预期失败、1 个通过；预期失败不是项目回归 |
| 安装版 CLI | 只读、审批修复、测试、回滚及缺依赖阻塞全部通过 |
| 安装版诊断 | 结构化定位与跨文件证据闭环通过 |
| 安装版面板 | stdio 只读预览、审批、取消与恢复通过 |
| 安装版修复 | 新回归在修复前失败、修复后通过，发送范围扩展与重启回滚通过 |
| 安装版独立服务 | 过期版本审批拒绝、事件重放、重启后恢复通过 |
| 安装版候选集 | 8 项可靠性探针和 3 项隐藏缺陷基线通过，原始候选文件保持不变 |
| VSIX | 使用本机已有 `@vscode/vsce@4.0.0` 离线打包，15 个文件；源码与媒体资源完整 |
| 真实 API | **用户自验通过**；本轮未重复调用真实 API |

执行环境：Windows，项目 `.venv` 中 Python 3.14，Node 24.18.0，npm 12.0.1。源码测试显式设置 `PYTHONPATH=src`；安装版检查清除该变量，确保加载实际安装包。示例基线出现本机既有 pytest 缓存权限警告，不影响预期缺陷结果。

## 用户真实 API 验收记录

用户原话：“我自行验证真实API通过，记录通过”。按此确认记录真实 API 调用通过；本轮不再发起付费模型请求，没有新增 API 费用。

此前候选集 01/02 的失败、超时和 API 排查记录保持原样，它们描述当时的实际运行。用户自验结果单独追加，不改写历史运行结果；本次没有获得四个冻结案例的新逐案产物，不生成新的自动修复成功率或根因准确率。历史证据见 [V2 候选集验收](ACCEPTANCE_V2.md) 与 [API 超时专项诊断](API_TIMEOUT_DIAGNOSIS.md)。

## 本轮补齐的交付项

- CI 在原有 Windows/Linux、Python 3.11–3.14 矩阵中增加安装版诊断、面板、修复、服务及候选集检查；使用 Bash 在任一脚本失败时停止。
- 新增 Windows/Linux 插件 CI，创建桥接测试所需的 `.venv`，执行 Node 测试及语法检查。
- 安装版服务检查使用当前 Python 安装前缀，消除硬编码 Windows 环境路径。
- `MANIFEST.in` 补入 docs，避免源码包 README 的文档链接缺失。
- 插件补齐 monorepo 仓库地址并修复打包时的文档链接解析；说明当前使用核心 0.7.0。
- Git 排除本机 debug 日志、ACL 备份、npm 缓存、路径配置和凭据；真实验收与恢复证据继续保留在本机忽略目录。

## 交付文件与复查

本轮构建产物保存在 `dist/full-acceptance-20261015/`：

- `mini_coding_agent-0.7.0-py3-none-any.whl`
- `mini_coding_agent-0.7.0.tar.gz`
- `mini-agent-panel-0.6.0.vsix`
- `SHA256SUMS.txt`（三个包的 SHA-256 校验值）

VSIX 为本机交付包，含现有解释器和配置文件的路径，不含密钥；跨机器使用需配置解释器。产物受 `dist/` 忽略规则保护；本轮推送源码、测试、CI 与验收文档，不创建 Marketplace 或 PyPI 发布。

主要本机证据目录：

- 源码测试：`.pytest-tmp/full-acceptance-20261015/`
- CLI：`.mini-agent/installed-cli/e19706bc26e5/`
- 诊断：`.mini-agent/installed-core3/2485c938721a/`
- 面板：`.mini-agent/wheel-panel-check/a1fe90fbac7b/`
- 修复：`.mini-agent/installed-core4/55c95a2ef74e/`
- 服务：`.mini-agent/installed-core6/5c25f3a433dd/`
- 离线候选集：`.pytest-tmp/installed-acceptance-52f9845364ce/offline-results.json`

Git 提交和远端状态以本轮最终交付消息及仓库历史为准。新补充的跨平台 CI 需要在推送后由 GitHub Actions 执行，本机结果不代表远端矩阵已运行通过。

## 远端矩阵首轮发现及修复

首轮推送 `075fe4c` 触发 [CI 38683499914](https://github.com/cycy12335/mini_agent/actions/runs/38683499914)。Windows/Linux 插件任务通过；Python 矩阵暴露新增验收和取消测试直接使用 `sys.executable`，而 runner 使用非虚拟环境 Python，不符合受控测试解释器要求。本机原本从 `.venv` 运行，因而未复现。

修复沿用项目已有的 `interpreter` fixture，为验收及真实 pytest 取消测试提供明确虚拟环境；CI 安装和验收统一放入 `.venv`。保持生产解释器校验规则，未放宽执行权限。首轮失败记录保留，后续复验以最终提交对应的 GitHub Actions 为准。

修复后本机定向执行 `tests/test_acceptance.py tests/test_service.py`，结果 **36 passed in 23.71s**；生产代码未改变。
