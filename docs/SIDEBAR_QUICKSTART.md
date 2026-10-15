# Mini Agent 侧边栏使用

本机已使用独立 Python 核心；插件 0.6.0 提供当前 VS Code 窗口内的侧边栏和模型/API 配置。

## 在侧边栏配置模型与 Key

1. 点击侧边栏顶部“设置”，或展开“模型与 API 配置”。
2. API 地址填写服务商的完整请求地址（包含 `/chat/completions` 或 `/responses` 对应路径），再选择匹配的协议。
3. 模型 ID 填写服务商提供的准确名称；之后可从输入框的曾用模型列表选择。
4. 首次输入 API Key，点击“保存配置”。成功后显示“已保存 Key”；输入框会清空，之后留空保存会保留 Key，输入新值则替换。
5. 任务模型选择“已配置的 API 模型”，输入问题并开始。保存配置本身不发送 API 请求；启动任务时仍需确认代码发送。

需要时展开“预算与推理选项”调整预算、输入/输出单价和推理强度。单次预算上限沿用核心的 ¥0.5 限制。更换模型请核对两项单价，不支持的推理参数请选择“不指定”。

面板配置优先于原配置文件中的模型设置；未保存过面板配置时沿用原有配置与凭据。点击“清除面板配置与 Key”后恢复原配置文件方式，不会删除原凭据文件。面板配置适用于当前 VS Code 用户的全部项目，活动任务期间不能修改。解释器设置仍可通过面板的“解释器设置”进入。

API Key 使用 [VS Code SecretStorage](https://code.visualstudio.com/api/references/vscode-api#SecretStorage) 保存，不回显、不进入 Webview 持久化状态，也不写入项目、普通设置或生成的配置文件。启动时仅通过独立 Agent 子进程的环境变量传递。临时配置位于插件存储目录，进程退出后清理；核心继续独立运行，无需更新 wheel。

## 日常使用

1. 在 VS Code 打开需要处理的项目。
2. 点击左侧活动栏的机器人图标 **Mini Agent**。不必打开插件源码目录，不必按 F5。
3. “当前仓库”自动填入工作区路径。多根工作区首次打开时优先使用当前编辑文件所属仓库，也可点击“更换”。
4. 输入 Bug 描述，例如“空列表会导致平均值函数崩溃，请定位原因并修复”。
5. 展开“运行选项”，需要写入时选择“确认后修改”；需要验证时选择授权测试模式并设置目标测试解释器。默认只读分析。
6. 点击“开始任务”。代码发送、修改和恢复需要你在侧边栏确认；先查看 Diff，再同意。

切换到资源管理器后任务继续执行；再次点击图标即可返回。Diff、报告和源码使用当前窗口编辑器标签页，不会创建新的 VS Code 窗口。关闭整个 VS Code 或重载扩展会中断当前任务，重新打开只能查看历史。

## 本机安装与配置

安装包：`D:\mini_agent\dist\mini-agent-panel-0.6.0.vsix`。

本机安装包附带路径配置：Python 为 `D:\mini_agent\.venv\Scripts\python.exe`，API 配置为 `D:\mini_agent\agent.api.example.json`。凭据内容不打入插件。VS Code 中显式设置的 `miniAgent.pythonPath`、`miniAgent.configPath` 始终优先。更换电脑后需要重新配置路径；本机包不宜作为通用发行包分发。

首次安装后若未出现图标，在 VS Code 通知中点击“重新加载”，或通过菜单“查看 → 命令面板”执行“Developer: Reload Window”。这只是安装后的刷新，日常通过图标进入。

如需重新安装，在 PowerShell 执行：

```powershell
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension D:\mini_agent\dist\mini-agent-panel-0.6.0.vsix --force
```

## 简单验收

1. 用 VS Code 打开 `D:\mini_agent\examples\buggy_project`，点击机器人图标，确认路径自动显示该目录。
2. 输入“分析 average([]) 为什么崩溃”，在运行选项中选择 Mock 和只读分析，点击“开始任务”。
3. 确认侧边栏出现执行事件和最终结果，源码没有变化。Mock 仅用于该 average 示例。
4. 切到资源管理器，再点击机器人图标，确认结果仍在；整个过程不出现新的 VS Code 窗口。

真实项目使用 API 模型。当前核心能力与限制以核心文档为准；侧边栏入口不意味着所有 Bug 均可自动修复成功。
