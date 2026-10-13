"""Read-only file tools and the future Agent run interface."""

import argparse
import json
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .policy import PolicyGuard
from .tools.files import FileTools, result_boundary
from .adapters.mock import MockAdapter
from .audit import AuditLog, DEFAULT_RUNS_ROOT
from .controller import AgentController
from .tools.registry import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="Mini Coding Agent — 阶段 2：离线 Mock 循环与修改提案。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    files = commands.add_parser("files", help="只读文件工具（输出 JSON）")
    files.add_argument("--project", type=Path, required=True, help="目标项目目录")
    files.add_argument("--config", type=Path, help="UTF-8 JSON 预算配置")
    operations = files.add_subparsers(dest="operation", required=True)
    listing = operations.add_parser("list", help="列出允许访问的候选文本文件")
    listing.add_argument("--path", default=".", help="相对目录")
    reading = operations.add_parser("read", help="读取 UTF-8 文本文件")
    reading.add_argument("path", help="相对文件路径")
    search = operations.add_parser("search", help="按字面量搜索文本")
    search.add_argument("query", help="区分大小写的搜索文本（非正则表达式）")
    search.add_argument("--path", default=".", help="相对目录")
    run = commands.add_parser("run", help="运行离线 Mock 示例，只记录提案")
    run.add_argument("--project", type=Path, required=True, help="目标项目目录")
    run.add_argument("--task", required=True, help="bug 描述")
    run.add_argument("--model", choices=("mock", "api"), default="mock",
                     help="预留模型选择；当前均不发起调用")
    run.add_argument("--config", type=Path, help="UTF-8 JSON 预算配置")
    permissions = run.add_mutually_exclusive_group()
    permissions.add_argument("--read-only", action="store_true", help="只读模式")
    permissions.add_argument("--allow-tests", action="store_true", help="显式允许未来的测试执行")
    run.add_argument("--test-python", type=Path, help="目标项目虚拟环境解释器")
    run.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT,
                     help="本地审计目录，必须位于目标项目之外（模型不能指定）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "files":
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            parser.error(str(exc))
        result = browse(args, config)
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return 0 if result.ok else 1
    try:
        policy = PolicyGuard(args.project)
        if not args.task.strip() or len(args.task) > 4096:
            parser.error("--task 需要 1–4096 个字符")
        config = load_config(args.config)
        if args.model != "mock":
            parser.error("真实 API 适配将在阶段 5 实现，当前仅支持 --model mock")
        if args.allow_tests or args.test_python is not None:
            parser.error("阶段 2 不支持测试执行；请移除 --allow-tests/--test-python")
        audit = AuditLog(args.runs_dir, policy.root)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print("Mock 为预设的 average 空列表演示，不是通用模型；本次只读并记录提案。")
    print(f"运行目录：{audit.directory}", flush=True)
    try:
        result = AgentController(MockAdapter(), ToolRegistry(FileTools(policy, config)), audit, config).run(args.task)
    except OSError:
        print("状态：failed；审计写入失败，无法保证报告完整。目标项目未修改。")
        return 1
    print(f"状态：{result.status} ({result.reason})")
    print(result.message)
    print("测试：未执行；修改：未应用。")
    print(f"报告：{result.report}")
    return result.exit_code


@result_boundary
def browse(args, config):
    tools = FileTools(PolicyGuard(args.project), config)
    if args.operation == "list":
        return tools.list_files(args.path)
    if args.operation == "read":
        return tools.read_file(args.path)
    return tools.search_text(args.query, args.path)
