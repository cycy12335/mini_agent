# Mini Agent

在 VS Code 侧边栏中分析和修复 Python 项目，支持代码定位、调用图、修改审批、原生 Diff、测试结果、取消任务和历史恢复。

## 安装

1. 安装 Python 3.11+ 和 Mini Agent 核心：在项目根目录运行 `python -m pip install . pytest`。
2. 在 VS Code 的扩展菜单中选择“从 VSIX 安装”，安装 Mini Agent 插件。
3. 在设置中将 `miniAgent.pythonPath` 设为已安装核心的 Python 可执行文件的绝对路径。

从源码打包及完整操作见 [侧边栏安装与使用](https://github.com/cycy12335/mini_agent/blob/main/docs/SIDEBAR_QUICKSTART.md)。

## 使用

1. 打开目标项目，点击左侧 **Mini Agent** 图标，仓库路径自动填入。
2. 展开“模型与 API 配置”，填写完整请求地址、协议、模型 ID 和 API Key，点击“保存配置”。
3. 输入问题，选择运行模式并启动。默认只读；修改和代码发送需要确认。
4. 查看 Diff 后同意或拒绝修改。授权测试模式需设置目标项目的测试解释器。

API Key 保存在 VS Code SecretStorage，留空保存会保留已有 Key，也可替换或清除。可使用 `miniAgent.configPath` 指定已有 JSON 配置；侧边栏保存的 API 配置优先。

“取消任务”会请求核心停止并保存已有结果。切换侧边栏后任务继续，关闭整个 VS Code 会中断任务。历史记录支持查看报告及预览恢复；恢复前会检查磁盘与未保存文件冲突。

插件运行在本机，不支持远程或虚拟工作区。Mock 模式仅用于附带的平均值示例；实际项目请选择 API 模型。
