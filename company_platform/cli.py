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
from .plugin_registry import RegistryError, load_registry
from .profiles import load_profile
from .runtime import RuntimeStore
from .server import serve


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def command_validate() -> int:
    root = project_root()
    registry = load_registry(root)
    profiles = [
        load_profile(root, registry, "company-manager"),
        load_profile(root, registry, "company-with-sales"),
    ]
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


def command_self_test() -> int:
    root = project_root()
    registry = load_registry(root)
    profile = load_profile(root, registry, "company-with-sales")
    with tempfile.TemporaryDirectory(prefix="agent4company-self-test-") as temporary:
        runtime = RuntimeStore(
            Path(temporary) / "runtime.db",
            registry,
            enabled_domains=profile.enabled_domains,
        )
        task = runtime.create_task("domain.sales.pipeline-review", "销售域空数据接入自检")
        if task["status"] != "waiting_approval" or len(task["approvals"]) != 1:
            raise RuntimeError("销售域工作流未到达审批边界")
        completed = runtime.decide_approval(task["approvals"][0]["approval_id"], "approved")
        if completed["status"] != "completed":
            raise RuntimeError("销售域工作流审批后未完成")
        if not runtime.list_audit_events():
            raise RuntimeError("审计事件未写入")
    print(json.dumps({"status": "ok", "self_test": "sales-domain-governed-dag"}, ensure_ascii=False))
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


def command_launch(pi_args: Sequence[str], profile_id: str = "company-manager") -> int:
    root = project_root()
    command = root / "node_modules/.bin/pi.CMD"
    if not command.is_file():
        raise RuntimeError("Pi 主智能核心尚未安装，请先运行 scripts/setup-windows.ps1")
    environment = dict(os.environ)
    profile = load_profile(root, load_registry(root), profile_id)
    resource_args = [
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-session",
        "--offline",
        "--tools",
        "company_capability_catalog,company_plan_workflow,company_check_domain_permissions",
        "--extension",
        str(root / "pi/extensions/company-workflow.ts"),
        "--skill",
        str(root / "pi/skills/manage-company"),
    ]
    if "domain.sales" in profile.enabled_domains:
        resource_args.extend(["--skill", str(root / "pi/skills/manage-sales-domain")])
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
    subparsers.add_parser("self-test", help="在临时数据库完成销售域受控流程自检")
    subparsers.add_parser("doctor", help="检查本机运行和桌面构建依赖")
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
