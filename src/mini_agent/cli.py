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
from .patching import PatchManager
from .tools.tests import TestRunner, validate_interpreter
from .adapters.http_api import HTTPAdapter, load_api_key
from .privacy import Redactor, ScopedFiles, disclosure_files
from .models import ToolError
from .repository import discover_repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="Mini Coding Agent：仓库探测、发送审批与修复验证。",
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
    project = commands.add_parser("project", help="只读探测 Python 仓库（输出 JSON，不调用模型）")
    project.add_argument("--project", type=Path, required=True)
    project.add_argument("--config", type=Path)
    symbols = commands.add_parser("symbols", help="只读 Python AST 定义、引用、调用和相关测试查询")
    symbols.add_argument("--project", type=Path, required=True)
    symbols.add_argument("--config", type=Path)
    symbols.add_argument("operation", choices=("definition", "references", "callers", "callees", "related-tests"))
    symbols.add_argument("symbol", help="符号短名、完整名称或查询返回的 ID")
    symbols.add_argument("--path", help="限定目标定义的相对文件路径")
    for command in ("inspect", "rollback"):
        stored = commands.add_parser(command, help="查看运行记录" if command == "inspect" else "预览并确认恢复")
        stored.add_argument("--run-id", required=True)
        stored.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT)
    history = commands.add_parser("history", help="列出历史任务与中断状态")
    history.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT)
    history.add_argument("--limit", type=int, default=100)
    events = commands.add_parser("events", help="按游标重放持久化审计事件")
    events.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT)
    events.add_argument("--run-id", required=True)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    commands.add_parser("serve", help="独立本地 JSONL 任务服务（stdin/stdout）")
    run = commands.add_parser("run", help="运行 Mock 或真实 API；发送和修改分别确认")
    run.add_argument("--project", type=Path, required=True, help="目标项目目录")
    run.add_argument("--task", required=True, help="bug 描述")
    run.add_argument("--model", choices=("mock", "api"), default="mock",
                     help="mock 离线演示；api 使用配置中的模型服务")
    run.add_argument("--send-path", action="append", help="API 可发送的相对文件或目录，可重复；默认列出所有合规候选文件供确认")
    run.add_argument("--config", type=Path, help="UTF-8 JSON 预算配置")
    permissions = run.add_mutually_exclusive_group()
    permissions.add_argument("--read-only", action="store_true", help="只读模式")
    permissions.add_argument("--allow-tests", action="store_true", help="允许执行可信项目的测试代码（不是安全沙箱）")
    run.add_argument("--test-python", type=Path, help="目标项目虚拟环境解释器；省略时探测项目本地环境")
    run.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT,
                     help="本地审计目录，必须位于目标项目之外（模型不能指定）")
    return parser


def main(argv: list[str] | None = None, *, interaction=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        from .service import serve
        return serve()
    if args.command in {"history", "events"}:
        from .history import history, replay
        try:
            result = history(args.runs_dir, limit=args.limit) if args.command == "history" else replay(AuditLog.open(args.runs_dir, args.run_id), after=args.after, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
    if args.command in {"inspect", "rollback"}:
        return stored_run(args, interaction=interaction)
    if args.command == "symbols":
        try:
            files = FileTools(PolicyGuard(args.project), load_config(args.config))
            operation = "find_" + args.operation.replace("-", "_")
            result = ToolRegistry(files).symbols.query(operation, args.symbol, args.path)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0 if result.ok else 1
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    if args.command == "project":
        try:
            overview = discover_repository(FileTools(PolicyGuard(args.project), load_config(args.config)))
            print(json.dumps(overview, ensure_ascii=False, indent=2))
            return 0
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    if args.command == "files":
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            parser.error(str(exc))
        result = browse(args, config)
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return 0 if result.ok else 1
    from .runtime import run_task
    try:
        return run_task(args, interaction=interaction, error=parser.error, confirm=confirm, controller_factory=AgentController)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False


def stored_run(args, *, interaction=None):
    from .runtime import recover_run
    return recover_run(args, interaction=interaction, confirm=confirm)


@result_boundary
def browse(args, config):
    tools = FileTools(PolicyGuard(args.project), config)
    if args.operation == "list":
        return tools.list_files(args.path)
    if args.operation == "read":
        return tools.read_file(args.path)
    return tools.search_text(args.query, args.path)
