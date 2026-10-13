"""Read-only file tools and the future Agent run interface."""

import argparse
import json
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .policy import PolicyGuard
from .tools.files import FileTools, result_boundary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="Mini Coding Agent — 阶段 1：只读文件工具与权限边界。",
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
    run = commands.add_parser("run", help="校验任务参数（Agent 执行尚未实现）")
    run.add_argument("--project", type=Path, required=True, help="目标项目目录")
    run.add_argument("--task", required=True, help="bug 描述")
    run.add_argument("--model", choices=("mock", "api"), default="mock",
                     help="预留模型选择；当前均不发起调用")
    run.add_argument("--config", type=Path, help="UTF-8 JSON 预算配置")
    permissions = run.add_mutually_exclusive_group()
    permissions.add_argument("--read-only", action="store_true", help="只读模式")
    permissions.add_argument("--allow-tests", action="store_true", help="显式允许未来的测试执行")
    run.add_argument("--test-python", type=Path, help="目标项目虚拟环境解释器")
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
        project = args.project.resolve(strict=True)
        if not project.is_dir():
            parser.error("--project 必须是已存在的目录")
        if not args.task.strip():
            parser.error("--task 不能为空")
        load_config(args.config)
        if args.allow_tests and args.test_python is None:
            parser.error("--allow-tests 需要指定 --test-python")
        if args.test_python is not None:
            interpreter = args.test_python.resolve(strict=True)
            if not interpreter.is_file():
                parser.error("--test-python 必须是已存在的解释器文件")
    except (OSError, RuntimeError, ConfigError) as exc:
        parser.error(str(exc))
    print(f"目标项目：{project}")
    print("参数与配置校验通过。阶段 1 尚未实现 Agent 循环，请使用 files 命令浏览项目。")
    print("未读取项目源码、调用模型、修改文件或运行项目测试。")
    print("状态：failed（功能尚未实现，不代表目标项目测试失败）")
    return 1


@result_boundary
def browse(args, config):
    tools = FileTools(PolicyGuard(args.project), config)
    if args.operation == "list":
        return tools.list_files(args.path)
    if args.operation == "read":
        return tools.read_file(args.path)
    return tools.search_text(args.query, args.path)
