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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="Mini Coding Agent — 阶段 4：授权测试与修复反馈。",
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
    for command in ("inspect", "rollback"):
        stored = commands.add_parser(command, help="查看运行记录" if command == "inspect" else "预览并确认恢复")
        stored.add_argument("--run-id", required=True)
        stored.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT)
    run = commands.add_parser("run", help="运行离线 Mock 示例；修改必须逐次确认")
    run.add_argument("--project", type=Path, required=True, help="目标项目目录")
    run.add_argument("--task", required=True, help="bug 描述")
    run.add_argument("--model", choices=("mock", "api"), default="mock",
                     help="预留模型选择；当前均不发起调用")
    run.add_argument("--config", type=Path, help="UTF-8 JSON 预算配置")
    permissions = run.add_mutually_exclusive_group()
    permissions.add_argument("--read-only", action="store_true", help="只读模式")
    permissions.add_argument("--allow-tests", action="store_true", help="允许执行可信项目的测试代码（不是安全沙箱）")
    run.add_argument("--test-python", type=Path, help="目标项目虚拟环境解释器")
    run.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_ROOT,
                     help="本地审计目录，必须位于目标项目之外（模型不能指定）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"inspect", "rollback"}:
        return stored_run(args)
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
        if args.allow_tests != (args.test_python is not None):
            parser.error("--allow-tests 与 --test-python 必须同时提供")
        interpreter = validate_interpreter(args.test_python) if args.allow_tests else None
        audit = AuditLog(args.runs_dir, policy.root)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print("Mock 为预设的 average 空列表演示，不是通用模型。")
    print("只读模式：仅显示提案。" if args.read_only else "每次修改先显示差异，再请求确认；默认拒绝。")
    print(f"运行目录：{audit.directory}", flush=True)
    try:
        result = AgentController(MockAdapter(), ToolRegistry(FileTools(policy, config)), audit, config,
                                 read_only=args.read_only, approve=lambda proposal: confirm("应用此提案？[y/N]: "),
                                 display=print, test_runner=TestRunner(policy, interpreter, config, authorized=True)
                                 if interpreter else None).run(args.task)
    except (OSError, ValueError):
        print("状态：failed；记录或执行失败，报告可能不完整。请检查运行目录中的 manifest.json 和备份。")
        return 1
    print(f"状态：{result.status} ({result.reason})")
    print(result.message)
    print("测试、应用与恢复状态请查看报告及 manifest.json。")
    print(f"报告：{result.report}")
    return result.exit_code


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False


def stored_run(args) -> int:
    try:
        audit = AuditLog.open(args.runs_dir, args.run_id)
        if args.command == "inspect":
            state = {"run_id": audit.run_id, "directory": str(audit.directory)}
            for name in ("summary.json", "manifest.json", "rollback.json"):
                if audit._path(name).exists():
                    state[name] = audit.read_json(name)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if not audit._path("manifest.json").exists():
            print("本次运行没有应用清单，无需恢复。")
            return 0
        manifest = audit.read_json("manifest.json")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("project"), str):
            raise ValueError("Invalid manifest project")
        policy = PolicyGuard(Path(manifest["project"]))
        patches = PatchManager(policy, audit.directory, read_only=False)
        files = patches.rollback_preview()
        print(f"恢复目标：{policy.root}")
        print("\n".join(files) if files else "没有待恢复的修改。")
        if not files:
            return 0
        approved = confirm("恢复上述文件？[y/N]: ")
        audit.record("rollback_approval", approved=approved, files=files)
        if not approved:
            print("已取消恢复。")
            return 3
        restored = patches.rollback(True)
        audit.record("rollback_finished", files=restored, status="rolled_back")
        audit.write_json("rollback.json", {"status": "rolled_back", "files": restored})
        print("恢复完成；原运行报告保留为历史记录，当前状态见 manifest.json。")
        return 0
    except KeyboardInterrupt:
        print("已取消；若在恢复过程中中断，请检查 manifest.json 后重试。")
        return 3
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"操作失败：{exc}")
        return 1


@result_boundary
def browse(args, config):
    tools = FileTools(PolicyGuard(args.project), config)
    if args.operation == "list":
        return tools.list_files(args.path)
    if args.operation == "read":
        return tools.read_file(args.path)
    return tools.search_text(args.query, args.path)
