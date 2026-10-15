# 可视化人工测试：照做即可

以下指令针对当前项目 `D:\mini_agent`。全程使用离线 Mock，不需要 API Key，不产生模型费用；操作的是复制的测试项目。插件 0.5.0 使用当前窗口侧边栏；最短使用说明见 [侧边栏快速开始](SIDEBAR_QUICKSTART.md)。

## 1. 准备（只需执行一次）

在 Windows PowerShell 粘贴：

```powershell
Set-Location D:\mini_agent
.\.venv\Scripts\python.exe -m mini_agent --version
.\.venv\Scripts\python.exe .\scripts\prepare_visual_test.py
```

预期：显示当前已安装的核心版本（本机为 `0.7.0`），最后显示 `Offline manual-test fixtures ready`。不要为测试侧边栏降级核心。
脚本已为测试项目配置 Python 路径和测试解释器，不需要手工修改设置。
如果 `.venv` 不存在，先执行 `py -3 -m venv .venv`。只有第 3 个“修复并测试”场景需要 pytest；当前项目环境已安装。其他电脑可执行 `.\.venv\Scripts\python.exe -m pip install "pytest>=8,<10"`。

## 2. 打开面板

1. VS Code 菜单“文件 → 打开文件夹”，打开 **`D:\mini_agent\examples\demo-visual\readonly`**。
2. 如弹出信任提示，确认这是自己的项目后选择信任。
3. 点击左侧活动栏的 **Mini Agent 机器人图标**。如安装后未出现图标，按快速开始中的说明刷新窗口。
4. 全程在当前窗口操作，不需要 F5 或新的开发宿主。
5. 展开“运行选项 · 模型与权限”。
6. 检查路径为 **`D:\mini_agent\examples\demo-visual\readonly`**。模型下拉选择 **`Mock 示例演示`**。每次启动前确认这个选择。

预期：面板显示“新任务”“运行记录”“开始任务”。不需要 npm install，不需要输入密钥。

## 3. 测试 A：只读分析（约 10 秒）

1. Bug 描述输入：**`平均值函数在空列表时崩溃，请修复为空列表返回 0。`**
2. 执行方式选择 **`只读分析`**，点击“开始任务”。
3. 等待状态显示 **`分析 / 修改提案`**。
4. 点击 **`Diff · src/calculator.py`**，应出现左右对比，右侧新增：

```python
    if not values:
        return 0
```

5. 返回 Mini Agent 面板，点击“查看报告”，报告应包含 `Status: proposed`。

通过条件：出现提案和 Diff，没有要求同意修改；原文件未修改。
可在 PowerShell 核实：

```powershell
Select-String -LiteralPath D:\mini_agent\examples\demo-visual\readonly\src\calculator.py -Pattern 'if not values'
```

预期：**没有输出**。

## 4. 测试 B：修复并验证（约 30 秒）

1. 回到面板，将路径改为 **`D:\mini_agent\examples\demo-visual\repair`**。
2. Bug 描述不变，模型仍选 Mock。
3. 执行方式选择 **`确认后修改，并授权运行测试`**，点击“开始任务”。
4. 等待显示 **`等待你的确认`** 和“确认应用修改”。先点击 `Diff · src/calculator.py` 查看改动，再返回面板点击 **`同意并继续`**。
5. 等待状态显示 **`修复已验证`**，测试对比中应显示 **已修复 1、剩余 0、新增失败 0**。
6. 点击“查看报告”，应包含 `Status: verified`。

通过条件：同意后才修改，最终显示修复已验证。可再次手动验证：

```powershell
Set-Location D:\mini_agent\examples\demo-visual\repair
D:\mini_agent\.venv\Scripts\python.exe -m pytest tests -q
```

预期：全部测试通过（`passed`），没有 `failed`。

## 5. 测试 C：取消（约 10 秒）

1. 将路径改为 **`D:\mini_agent\examples\demo-visual\cancel`**。
2. 执行方式选择 **`确认后修改，不运行测试`**，点击“开始任务”。
3. 出现“等待你的确认”后，**不要点击同意**，点击 **`取消任务`**。
4. 等待状态显示 **`已取消`**。

通过条件：取消后源码未改变。核实命令：

```powershell
Select-String -LiteralPath D:\mini_agent\examples\demo-visual\cancel\src\calculator.py -Pattern 'if not values'
```

预期：**没有输出**。

## 6. 可选：拒绝与面板重开

路径改为 `D:\mini_agent\examples\demo-visual\decline`，选 Mock + 确认后修改、不运行测试。
开始后，在等待确认时关闭 Mini Agent 标签，再用 `Ctrl+Shift+P` 重开面板。
预期：仍显示待确认提案。点击“拒绝”，最终为“已取消”，文件未修改。

## 7. 如何报告结果

### 新阶段测试 D：恢复已修复文件

1. 在“最近运行”下拉选择测试 B 的记录。检查“仓库”为 `D:\mini_agent\examples\demo-visual\repair`，历史结果为“修复已验证”。
2. 点击“预览恢复”。预览不会修改源码，面板应列出 `src/calculator.py`。
3. 点击“确认恢复”。等待“恢复清单状态”显示“本次修改已恢复，恢复后未重新测试”。
4. 原历史结果仍为“修复已验证”，这是当时的结果，恢复后不能当作当前验证。
5. PowerShell 执行下面命令，应没有输出：

```powershell
Select-String -LiteralPath D:\mini_agent\examples\demo-visual\repair\src\calculator.py -Pattern 'if not values'
```

关闭面板再重开，预期仍能查看该历史记录及“已恢复”状态。

### 新阶段测试 E：恢复冲突不覆盖用户内容

1. 重新完成测试 B（D 已恢复示例，B 可以再次产生修改）。
2. 在修复后的 `src/calculator.py` 文件末尾添加一行 `# my manual change` 并保存。
3. 返回该运行记录，点击“预览恢复”。
4. 预期显示 `HASH_CONFLICT`，没有“确认恢复”提示；文件中的 `# my manual change` 保留。

之后用本指南最后的 `--reset` 命令重置测试副本即可。不要为通过测试而强制覆盖冲突文件。

### 新阶段测试 F：调用图、定位证据与代码跳转

1. 在 PowerShell 执行：

```powershell
Set-Location D:\mini_agent
.\.venv\Scripts\python.exe .\scripts\prepare_visual_diagnosis.py
```

2. 脚本最后会打印一个完整运行目录，例如 `.mini-agent\visual-diagnosis\runs\2026...`。复制这条路径。
3. 关闭旧开发宿主，在插件文件夹按 F5 重开；打开 Mini Agent 面板，点击“导入运行记录”，将上一步的完整目录粘贴到文件夹选择框，选中该运行编号文件夹。
4. 预期：“定位与证据”有一个已获支持的假设和根因描述；“相关调用关系”显示 `checkout → mean`。选择“聚焦符号”可以只看上下游。
5. 点击图中的 checkout 节点，预期打开 `src/shop/entry.py`；点击 mean，预期打开 `src/shop/maths.py`。
6. 展开证据，点击“打开代码位置”，预期跳到对应代码。预算应显示“没有模型费用记录”，因为这次没有调用 API。

这个示例由离线脚本通过真实核心生成，只验证定位记录的展示，不是通用模型修复能力测试。

### 新阶段测试 G：历史位置过期保护

1. 在测试 F 打开的 maths.py 末尾加一行 `# manual edit`，保存。
2. 返回面板，再点击 mean 节点或该文件的证据按钮。
3. 预期提示“代码已变化，历史位置可能过期”，不会继续跳转。导入记录的“预览恢复”按钮始终不可用。
4. 点击“返回本地记录”，应恢复本地运行列表。

### 报告格式

只需回复：`A/B/C/D/E/F/G 各项通过或失败`。仅验收本阶段时回复 F/G 即可。
如果失败，补充最后看到的状态，并点击“查看运行输出”，复制最后 10 行；无需提供密钥。

常见问题：

| 现象 | 处理 |
| --- | --- |
| 找不到面板命令 | 确认是在第二个“扩展开发宿主”窗口；第一个窗口只负责 F5 |
| 找不到 mini_agent.panel | 重新执行第 1 节的 wheel 安装命令，关闭第二个窗口，再按 F5 |
| 显示需要 configPath | 模型仍选了 API，请改成 Mock |
| 仓库中有未保存文件 | 保存测试项目文件，重新启动；只读 Diff 快照无需保存 |
| 测试失败，提示 pytest 不存在 | 给 `D:\mini_agent\.venv\Scripts\python.exe` 安装 pytest，再试 |
| 再次测试 B 没有提案 | B 已修复该副本；执行下面的重置命令后再测 |

只重置本指南创建的示例副本（会覆盖这些副本中的样例源码与测试，不碰原始示例）：

```powershell
Set-Location D:\mini_agent
.\.venv\Scripts\python.exe .\scripts\prepare_visual_test.py --reset
```

Mock 通过只证明界面和修复流程可用，不证明真实模型已能修复任意 Bug。API 调试另行测试。
