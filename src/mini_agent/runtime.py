"""Client-independent assembly and execution of tasks and recovery."""

import json
from pathlib import Path
from contextlib import nullcontext

from .locking import RepositoryLease
from .history import TaskLifecycle
from .config import load_config
from .policy import PolicyGuard
from .tools.files import FileTools
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


def invalid_request(message):
    raise ValueError(message)


def run_task(args, *, interaction=None, error=invalid_request, confirm=lambda _: False, controller_factory=AgentController):
    policy = PolicyGuard(args.project)
    config = load_config(args.config)
    output = interaction.display if interaction else print
    try:
        lease = nullcontext() if args.read_only else RepositoryLease(
            policy.root, cancelled=interaction.cancelled if interaction else lambda: False,
            timeout=config.run_timeout_seconds)
        with lease:
            return _run_task(args, interaction=interaction, error=error, confirm=confirm, controller_factory=controller_factory)
    except KeyboardInterrupt:
        output("状态：cancelled；等待仓库锁时已取消。")
        return 3
    except ToolError as exc:
        output(f"状态：{exc.code.lower()}；{exc}")
        return 3 if exc.code == "CANCELLED" else 1


def _run_task(args, *, interaction, error, confirm, controller_factory):
    try:
        policy = PolicyGuard(args.project)
        if not args.task.strip() or len(args.task) > 4096:
            error("--task 需要 1–4096 个字符")
        config = load_config(args.config)
        if args.model == "api" and config.api is None:
            error("--model api 需要 --config 中的 api 配置；参考 agent.api.example.json")
        if args.model == "mock" and args.send_path:
            error("--send-path 仅用于 --model api")
        if args.test_python is not None and not args.allow_tests:
            error("--test-python 需要 --allow-tests 授权")
        files = FileTools(policy, config)
        overview = discover_repository(FileTools(policy, config))
        interpreter = None
        if args.allow_tests:
            selected = args.test_python or overview["selected_interpreter"]
            if selected is None:
                error("无法唯一选择项目虚拟环境，请创建 .venv 或显式指定 --test-python；不会自动安装依赖")
            interpreter = validate_interpreter(Path(selected))
        redactor = None
        manifest = None
        adapter = MockAdapter()
        if args.model == "api":
            key = load_api_key(config.api)
            redactor = Redactor([key])
            manifest = disclosure_files(files, args.send_path)
            # Do not disclose credential-like filenames/endpoint configuration either.
            if redactor.value(manifest) != manifest or redactor.text(config.api.endpoint) != config.api.endpoint:
                error("发送配置或文件清单包含疑似凭据，请先移除")
            files = ScopedFiles(policy, config, manifest, redactor)
            adapter = HTTPAdapter(config.api, key=key, timeout=config.model_timeout_seconds, redactor=redactor)
        context_files = ScopedFiles(policy, config, manifest, redactor) if manifest is not None else FileTools(policy, config)
        project_context = discover_repository(context_files, local=False)
        audit = AuditLog(args.runs_dir, policy.root, redactor=redactor,
                         observer=interaction.on_audit if interaction else None)
    except (OSError, RuntimeError, ValueError) as exc:
        error(str(exc))
    lifecycle = TaskLifecycle(audit, policy.root)
    with lifecycle:
        return _execute_task(args, interaction, error, confirm, controller_factory, policy, config, files, overview, interpreter, redactor, manifest, adapter, project_context, audit)


def _execute_task(args, interaction, error, confirm, controller_factory, policy, config, files, overview, interpreter, redactor, manifest, adapter, project_context, audit):
    if interaction:
        interaction.attach(audit, config.run_timeout_seconds, redactor)
    output = interaction.display if interaction else print
    try:
        audit.write_json("repository.json", overview)
        audit.record("repository_discovered", language=overview["language"],
                     source_roots=overview["source_roots"], warnings=overview["warnings"],
                     truncated=overview["truncated"])
    except (OSError, ValueError):
        output("状态：failed；无法保存仓库概览，未请求模型或执行测试。")
        return 1
    output(f"仓库探测：{overview['language']}；源码根：{', '.join(overview['source_roots']) or '未识别'}；测试文件：{len(overview['test_files'])}。")
    if overview["warnings"]:
        output("探测提示：" + ", ".join(item["code"] for item in overview["warnings"]))
    if interpreter:
        output(f"测试解释器：{interpreter}")
    if args.model == "mock":
        output("Mock 为预设的 average 空列表演示，不是通用模型。")
    output("只读模式：仅显示提案。" if args.read_only else "每次修改先显示差异，再请求确认；默认拒绝。")
    if interaction:
        output(f"运行目录：{audit.directory}")
    else:
        print(f"运行目录：{audit.directory}", flush=True)
    def approve_disclosure():
        output(f"模型服务：{config.api.endpoint}\n协议：{config.api.protocol}\n模型：{config.api.model}")
        output(f"本次全部模型请求及重试的合计预算：¥{config.api.max_run_cost_cny:.2f}；发送前按输入估计及最大输出预留。")
        output("允许模型按需读取的文件（不会自动上传全部内容）：\n" + "\n".join(manifest))
        output("将发送任务、上述范围内按需读取的内容、工具结果及已授权测试的输出；测试输出可能包含项目运行产生的其他内容。")
        output("创建新文件的提案会明确列出新增发送范围；批准创建也授权后续按需读取该新文件。API 请求可能产生费用；日志不保存凭据或隐藏推理。")
        approved = interaction.request("disclosure", {"endpoint": config.api.endpoint,
            "model": config.api.model, "protocol": config.api.protocol, "files": manifest,
            "budget_cny": config.api.max_run_cost_cny,
            "notice": "将发送任务、按需读取的源码、工具结果和已授权测试输出；API 可能产生费用。"}) if interaction else confirm("同意向该服务发送以上数据范围？[y/N]: ")
        audit.record("disclosure_approval", endpoint=config.api.endpoint, model=config.api.model,
                     protocol=config.api.protocol, files=manifest, approved=approved)
        adapter.authorized = approved
        return approved
    try:
        result = controller_factory(adapter, ToolRegistry(files), audit, config,
                                 read_only=args.read_only, approve=interaction.approve if interaction else lambda proposal: confirm("应用此提案？[y/N]: "),
                                 cancelled=interaction.cancelled if interaction else lambda: False,
                                 display=output, test_runner=TestRunner(policy, interpreter, config, authorized=True)
                                 if interpreter else None, before_run=approve_disclosure if manifest is not None else None,
                                 project_files=manifest, project_context=project_context,
                                 proposal_observer=interaction.preview if interaction else None,
                                 unsaved_paths=interaction.unsaved_paths if interaction else lambda: set(),
                                 approve_test_changes=(lambda proposal: interaction.request("test_expectation", {
                                     "diff": proposal.diff, "files": list(proposal.expectation_changes), "reason": proposal.test_change_reason}))
                                     if interaction else lambda proposal: confirm("单独批准上述测试预期字面量调整？[y/N]: ")).run(args.task)
    except (OSError, ValueError):
        output("状态：failed；记录或执行失败，报告可能不完整。请检查运行目录中的 manifest.json 和备份。")
        return 1
    output(f"状态：{result.status} ({result.reason})")
    output(result.message)
    output("测试、应用与恢复状态请查看报告及 manifest.json。")
    output(f"报告：{result.report}")
    return result.exit_code



def recover_run(args, *, interaction=None, confirm=lambda _: False) -> int:
    if args.command == "rollback":
        from .locking import RepositoryLease
        try:
            audit = AuditLog.open(args.runs_dir, args.run_id)
            if audit._path("manifest.json").exists():
                project = PolicyGuard(Path(audit.read_json("manifest.json")["project"])).root
                with RepositoryLease(project, cancelled=interaction.cancelled if interaction else lambda: False, timeout=600):
                    return _stored_run(args, interaction=interaction, confirm=confirm)
        except KeyboardInterrupt:
            return 3
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            (interaction.display if interaction else print)(f"操作失败：{exc}")
            return 3 if getattr(exc, "code", None) == "CANCELLED" else 1
    return _stored_run(args, interaction=interaction, confirm=confirm)


def _stored_run(args, *, interaction=None, confirm=lambda _: False) -> int:
    output = interaction.display if interaction else print
    try:
        audit = AuditLog.open(args.runs_dir, args.run_id)
        if interaction:
            audit.redactor = Redactor()
            audit.observer = interaction.on_audit
            interaction.attach(audit, 600, audit.redactor)
        if args.command == "inspect":
            from .history import task_status
            state = {"run_id": audit.run_id, "directory": str(audit.directory), "task": task_status(audit)}
            for name in ("summary.json", "manifest.json", "rollback.json", "repository.json", "diagnosis.json", "validation.json"):
                if audit._path(name).exists():
                    state[name] = audit.read_json(name)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if not audit._path("manifest.json").exists():
            output("本次运行没有应用清单，无需恢复。")
            if interaction:
                interaction.emit("recovery_result", status="nothing_to_restore", files=[])
            return 0
        manifest = audit.read_json("manifest.json")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("project"), str):
            raise ValueError("Invalid manifest project")
        policy = PolicyGuard(Path(manifest["project"]))
        patches = PatchManager(policy, audit.directory, read_only=False)
        if interaction:
            patches.unsaved_paths = interaction.unsaved_paths
        files = patches.rollback_preview()
        output(f"恢复目标：{policy.root}")
        output("\n".join(files) if files else "没有待恢复的修改。")
        if not files:
            if interaction:
                interaction.emit("recovery_result", status="nothing_to_restore", files=[])
            return 0
        if interaction:
            audit.record("rollback_preview", files=files)
            approved = interaction.request("rollback", {"project": str(policy.root), "files": files,
                "manifest_sha256": patches._manifest_hash,
                "notice": "恢复上述文件到本次运行修改前的内容。原报告保留历史结果，恢复后需重新测试。"})
        else:
            approved = confirm("恢复上述文件？[y/N]: ")
        audit.record("rollback_approval", approved=approved, files=files)
        if not approved:
            output("已取消恢复。")
            if interaction:
                interaction.emit("recovery_result", status="cancelled", files=[])
            return 3
        if interaction and interaction.cancelled():
            raise ToolError("CANCELLED", "恢复已取消，尚未写入")
        restored = patches.rollback(True)
        audit.record("rollback_finished", files=restored, status="rolled_back")
        audit.write_json("rollback.json", {"status": "rolled_back", "files": restored})
        output("恢复完成；原运行报告保留为历史记录，当前状态见 manifest.json。")
        if interaction:
            interaction.emit("recovery_result", status="rolled_back", files=restored)
        return 0
    except KeyboardInterrupt:
        output("已取消；若在恢复过程中中断，请检查 manifest.json 后重试。")
        return 3
    except (OSError, ValueError, RuntimeError) as exc:
        output(f"操作失败：{exc}")
        if interaction:
            if "audit" in locals():
                try:
                    audit.record("rollback_cancelled" if getattr(exc, "code", None) == "CANCELLED" else "rollback_failed",
                                 error_code=getattr(exc, "code", "RECOVERY_ERROR"), message=str(exc))
                except (OSError, ValueError):
                    pass
            interaction.emit("recovery_result", status="cancelled" if getattr(exc, "code", None) == "CANCELLED" else "failed",
                             error_code=getattr(exc, "code", "RECOVERY_ERROR"), message=str(exc))
        return 3 if getattr(exc, "code", None) == "CANCELLED" else 1
