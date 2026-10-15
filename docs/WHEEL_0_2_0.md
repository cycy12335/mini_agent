# Wheel 0.2.0 本地交付

日期：2026-10-14。

产物：`dist/mini_coding_agent-0.2.0-py3-none-any.whl`。包含 `mini_agent.panel`、当前 Agent 核心与仓库探测模块。Python 要求仍为 3.11+，无运行时第三方依赖；执行测试需要 pytest。
这是本地构建与安装，尚未上传 GitHub Releases 或 PyPI。旧 0.1.0 文件保留供历史使用。

## 已完成

- pyproject.toml 与 mini_agent.__version__ 同步到 0.2.0。
- 独立构建 wheel；检查包内模块与源码一致，不包含 credentials 文件。
- 在不使用 system-site-packages 的独立虚拟环境安装 wheel，不使用 editable 安装。
- `check_installed_panel.py` 通过：只读提案、批准修改、取消；不联网，不调用模型。
- 当前 `.venv` 已卸载旧 0.1.0，安装新 wheel；`--version` 输出 0.2.0。
- `check_installed_cli.py` 通过：只读、批准修复、真实测试、恢复、缺依赖停止。
- `prepare_visual_test.py` 已创建人工测试副本和工作区设置，原始 buggy_project 未修改。

## 安装或重装

```powershell
Set-Location D:\mini_agent
.\.venv\Scripts\python.exe -m pip install --no-index --force-reinstall .\dist\mini_coding_agent-0.2.0-py3-none-any.whl
.\.venv\Scripts\python.exe -m mini_agent --version
```

预期输出：0.2.0。wheel 不包含 VS Code 插件本身，插件仍位于仓库 extensions/vscode。
启动与人工检查见 [清晰步骤](VISUALIZATION_MANUAL_TEST.md)。包校验值见 `dist/SHA256SUMS-0.2.0.txt`。
