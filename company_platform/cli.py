from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import __version__
from .library import search_library_read_only
from .plugin_registry import RegistryError, load_registry
from .profiles import CompanyProfile, load_profile, resolve_profile_skill_directories
from .runtime import RuntimeStore
from .server import serve


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def command_validate() -> int:
    root = project_root()
    registry = load_registry(root)
    profiles = [
        load_profile(root, registry, source.parent.name)
        for source in sorted((root / "profiles").glob("*/profile.json"))
    ]
    if not profiles or all(profile.id != "company-manager" for profile in profiles):
        raise RuntimeError("缺少公司级默认 Profile：company-manager")
    for profile in profiles:
        resolve_profile_skill_directories(root, registry, profile)
    print(
        json.dumps(
            {
                "status": "ok",
                "plugins": len(registry.plugins),
                "platform_capabilities": len(registry.platform_capabilities),
                "business_domains": len(registry.business_domains),
                "workflows": len(registry.workflows),
                "profiles": len(profiles),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _verification_profiles(root: Path, registry) -> list[CompanyProfile]:
    profiles: list[CompanyProfile] = []
    for source in sorted((root / "profiles").glob("*/profile.json")):
        profile = load_profile(root, registry, source.parent.name)
        if profile.default_workflow is not None:
            profiles.append(profile)
    if not profiles:
        raise RuntimeError("没有声明默认受控工作流的验证 Profile")
    return profiles


def command_self_test() -> int:
    root = project_root()
    registry = load_registry(root)
    profiles = _verification_profiles(root, registry)
    verified: list[dict[str, str | int]] = []
    with tempfile.TemporaryDirectory(prefix="agent4company-self-test-") as temporary:
        database = Path(temporary) / "runtime.db"
        for profile in profiles:
            workflow_id = profile.default_workflow
            assert workflow_id is not None
            workflow = registry.workflows[workflow_id]
            plugin = registry.plugins[workflow.plugin]
            runtime = RuntimeStore(database, registry, enabled_domains=profile.enabled_domains)
            task = runtime.create_task(workflow_id, f"{plugin.display_name}空数据接入自检")
            approval_count = 0
            max_approvals = max(1, len(workflow.nodes) * 2)
            while task["status"] == "waiting_approval":
                pending = [
                    approval
                    for approval in task["approvals"]
                    if approval["decision"] == "pending"
                ]
                if not pending:
                    raise RuntimeError(f"验证工作流等待审批但没有待处理项：{workflow_id}")
                for approval in pending:
                    approval_count += 1
                    if approval_count > max_approvals:
                        raise RuntimeError(f"验证工作流审批次数超过节点安全上限：{workflow_id}")
                    task = runtime.decide_approval(approval["approval_id"], "approved")
                    if task["status"] != "waiting_approval":
                        break
            if task["status"] != "completed":
                raise RuntimeError(f"验证工作流未完成：{workflow_id}（{task['status']}）")
            if not runtime.list_audit_events():
                raise RuntimeError("审计事件未写入")
            verified.append(
                {
                    "profile_id": profile.id,
                    "workflow_id": workflow_id,
                    "approval_count": approval_count,
                }
            )
    print(
        json.dumps(
            {"status": "ok", "self_test": "governed-domain-dag", "verified": verified},
            ensure_ascii=False,
        )
    )
    return 0


def command_doctor() -> int:
    root = project_root()
    registry = load_registry(root)
    load_profile(root, registry, "company-manager")
    checks = {
        "python": sys.version_info >= (3, 11),
        "plugins": bool(registry.plugins),
        "company_profile": (root / "profiles/company-manager/profile.json").is_file(),
        "ui": (root / "ui/index.html").is_file(),
        "tauri_manifest": (root / "desktop/src-tauri/Cargo.toml").is_file(),
        "node": shutil.which("node") is not None,
        "pnpm": shutil.which("pnpm") is not None,
        "cargo": shutil.which("cargo") is not None,
    }
    print(json.dumps({"status": "ok" if all(checks.values()) else "attention", "checks": checks}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


def command_library_search(
    query: str,
    *,
    limit: int = 8,
    runtime_dir: Path | str | None = None,
) -> int:
    """Return bounded evidence for the Pi read-only library tool.

    Confidential and highly confidential material is deliberately excluded until a model
    disclosure policy and per-use owner confirmation are implemented.
    """
    root = project_root()
    runtime_root = Path(runtime_dir or root / "runtime").resolve()
    requested_limit = max(1, min(int(limit), 20))
    database_path = runtime_root / "company-platform.db"
    results = search_library_read_only(
        database_path,
        query,
        confidentiality="internal",
        limit=requested_limit,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "query": query,
                "results": results,
                "disclosure_policy": (
                    "仅返回内部资料的证据片段；保密和高度保密资料需要后续逐次披露确认。"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_launch(pi_args: Sequence[str], profile_id: str = "company-manager") -> int:
    root = project_root()
    command = root / "node_modules/.bin/pi.CMD"
    if not command.is_file():
        raise RuntimeError("Pi 主智能核心尚未安装，请先运行 scripts/setup-windows.ps1")
    environment = dict(os.environ)
    registry = load_registry(root)
    profile = load_profile(root, registry, profile_id)
    skill_directories = resolve_profile_skill_directories(root, registry, profile)
    resource_args = [
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-session",
        "--offline",
        "--tools",
        "company_capability_catalog,company_plan_workflow,company_check_domain_permissions,company_library_search",
        "--extension",
        str(root / "pi/extensions/company-workflow.ts"),
    ]
    for directory in skill_directories:
        resource_args.extend(["--skill", str(directory)])
    environment["AGENT4COMPANY_PROFILE"] = profile.id
    environment["PI_CODING_AGENT_DIR"] = str(root / ".pi/company-runtime/pi-agent")
    result = subprocess.run(
        [str(command), *resource_args, *pi_args], cwd=root, env=environment, check=False
    )
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m company_platform", description="Agent4Company 本地管理命令")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="校验插件、依赖、DAG、权限和审批边界")
    subparsers.add_parser("self-test", help="在临时数据库完成默认业务域受控流程自检")
    subparsers.add_parser("doctor", help="检查本机运行和桌面构建依赖")
    library_search_parser = subparsers.add_parser(
        "library-search", help="为 Pi 返回受限的本机资料证据"
    )
    library_search_parser.add_argument("--query", required=True)
    library_search_parser.add_argument("--limit", type=int, default=8)
    library_search_parser.add_argument("--runtime-dir")
    serve_parser = subparsers.add_parser("serve", help="启动本地公司工作台")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8766)
    serve_parser.add_argument("--runtime-dir")
    serve_parser.add_argument("--open-browser", action="store_true")
    serve_parser.add_argument(
        "--profile",
        default=os.environ.get("AGENT4COMPANY_PROFILE", "company-manager"),
    )
    launch_parser = subparsers.add_parser("launch", help="启动 Pi 主智能核心")
    launch_parser.add_argument(
        "--profile",
        default=os.environ.get("AGENT4COMPANY_PROFILE", "company-manager"),
    )
    launch_parser.add_argument("pi_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate":
            return command_validate()
        if arguments.command == "self-test":
            return command_self_test()
        if arguments.command == "doctor":
            return command_doctor()
        if arguments.command == "library-search":
            return command_library_search(
                arguments.query,
                limit=arguments.limit,
                runtime_dir=arguments.runtime_dir,
            )
        if arguments.command == "serve":
            serve(
                project_root=project_root(),
                runtime_dir=arguments.runtime_dir,
                host=arguments.host,
                port=arguments.port,
                open_browser=arguments.open_browser,
                profile_id=arguments.profile,
            )
            return 0
        if arguments.command == "launch":
            pi_args = list(arguments.pi_args)
            if pi_args and pi_args[0] == "--":
                pi_args = pi_args[1:]
            return command_launch(pi_args, arguments.profile)
        parser.error("未知命令")
    except (RegistryError, RuntimeError, OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130
    return 2
