from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .plugin_registry import PluginRegistry, load_registry
from .profiles import CompanyProfile, load_profile
from .runtime import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RuntimeIdentity,
    RuntimeStore,
)


MAX_REQUEST_BYTES = 64 * 1024
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


@dataclass(frozen=True)
class ServerContext:
    project_root: Path
    ui_root: Path
    registry: PluginRegistry
    profile: CompanyProfile
    runtime: RuntimeStore
    session_token: str
    host: str
    port: int


class CompanyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], context_factory: Any) -> None:
        super().__init__(address, CompanyRequestHandler)
        self.context = context_factory(self.server_address[1])


class CompanyRequestHandler(BaseHTTPRequestHandler):
    server: CompanyHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep console output concise while preserving standard request evidence.
        super().log_message("[公司工作台] " + format, *args)

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def _write_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, status: HTTPStatus, message: str) -> None:
        self._write_json({"status": "error", "message": message}, status)

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("请求必须使用 application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ValueError("请求缺少有效 Content-Length")
        length = int(raw_length)
        if length < 2 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求大小超出限制")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("请求 JSON 无效") from error
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def _valid_local_request(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = {
            f"127.0.0.1:{self.server.server_address[1]}",
            f"localhost:{self.server.server_address[1]}",
        }
        return host in allowed

    def _require_local_request(self) -> bool:
        if self._valid_local_request():
            return True
        self._write_error(HTTPStatus.BAD_REQUEST, "仅接受本机工作台请求")
        return False

    def _require_session(self) -> bool:
        if not self._require_local_request():
            return False
        if not secrets.compare_digest(
            self.headers.get("X-Company-Session", ""), self.server.context.session_token
        ):
            self._write_error(HTTPStatus.FORBIDDEN, "工作台会话无效，请刷新页面")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_local_request():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._write_json(
                    {
                        "status": "ok",
                        "product_id": "agent4company",
                        "profile_id": self.server.context.profile.id,
                        "desktop_route": True,
                    }
                )
                return
            if parsed.path == "/api/bootstrap":
                self._write_json(self._bootstrap())
                return
            if parsed.path == "/api/tasks":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["20"])[0])
                self._write_json({"tasks": self.server.context.runtime.list_tasks(limit)})
                return
            if parsed.path.startswith("/api/tasks/"):
                task_id = parsed.path.removeprefix("/api/tasks/")
                self._write_json({"task": self.server.context.runtime.get_task(task_id)})
                return
            if parsed.path == "/api/approvals":
                self._write_json(
                    {"approvals": self.server.context.runtime.list_pending_approvals()}
                )
                return
            if parsed.path == "/api/audit":
                self._write_json({"events": self.server.context.runtime.list_audit_events()})
                return
            if parsed.path == "/api/plugins":
                self._write_json(self._profile_catalog())
                return
            self._serve_static(parsed.path)
        except NotFoundError as error:
            self._write_error(HTTPStatus.NOT_FOUND, str(error))
        except (ValueError, ConflictError) as error:
            self._write_error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:  # defensive HTTP boundary
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"工作台处理失败：{error}")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_session():
            return
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/tasks":
                allowed = {"workflow_id", "title", "project_id"}
                if set(payload) - allowed:
                    raise ValueError("任务请求包含未知字段")
                task = self.server.context.runtime.create_task(
                    str(payload.get("workflow_id", "")),
                    str(payload.get("title", "")),
                    project_id=(str(payload["project_id"]) if payload.get("project_id") else None),
                    identity=RuntimeIdentity(),
                )
                self._write_json({"status": "ok", "task": task}, HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/approvals/") and parsed.path.endswith("/decision"):
                approval_id = parsed.path.removeprefix("/api/approvals/").removesuffix("/decision")
                allowed = {"decision", "reason"}
                if set(payload) - allowed:
                    raise ValueError("审批请求包含未知字段")
                task = self.server.context.runtime.decide_approval(
                    approval_id,
                    str(payload.get("decision", "")),
                    reason=str(payload.get("reason", "")),
                    identity=RuntimeIdentity(),
                )
                self._write_json({"status": "ok", "task": task})
                return
            self._write_error(HTTPStatus.NOT_FOUND, "接口不存在")
        except NotFoundError as error:
            self._write_error(HTTPStatus.NOT_FOUND, str(error))
        except PermissionDeniedError as error:
            self._write_error(HTTPStatus.FORBIDDEN, str(error))
        except ConflictError as error:
            self._write_error(HTTPStatus.CONFLICT, str(error))
        except ValueError as error:
            self._write_error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:  # defensive HTTP boundary
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"工作台处理失败：{error}")

    def _bootstrap(self) -> dict[str, Any]:
        context = self.server.context
        registry = context.registry
        profile = context.profile
        catalog = self._profile_catalog()
        domain_notice = (
            "销售管理域已在当前验证组合中启用，可用空数据流程验证审批和审计。"
            if "domain.sales" in profile.enabled_domains
            else "销售管理域已安装但未在默认组合中启用；公司核心可独立运行。"
        )
        return {
            "product": {
                "id": "agent4company",
                "name": "公司管理平台",
                "edition": "第一阶段 · 本地工作台",
                "company_id": context.runtime.company_id,
            },
            "profile": profile.public_summary(),
            "identity": {
                "actor_id": "local-admin",
                "display_name": "本地管理员",
                "role": "company-admin",
                "mode": "第一阶段单人模式",
            },
            "session_token": context.session_token,
            "metrics": context.runtime.dashboard(),
            "navigation": [
                {"id": "overview", "label": "公司总览", "icon": "home"},
                {"id": "tasks", "label": "任务中心", "icon": "tasks"},
                {"id": "approvals", "label": "审批中心", "icon": "approval"},
                {"id": "projects", "label": "项目空间", "icon": "projects"},
                {"id": "knowledge", "label": "知识与文件", "icon": "knowledge"},
                {"id": "domains", "label": "业务域", "icon": "domains"},
                {"id": "audit", "label": "审计记录", "icon": "audit"},
                {"id": "settings", "label": "平台设置", "icon": "settings"},
            ],
            "platform_capabilities": [
                plugin.public_summary() for plugin in registry.platform_capabilities
            ],
            "business_domains": catalog["business_domains"],
            "workflows": catalog["workflows"],
            "tasks": context.runtime.list_tasks(),
            "approvals": context.runtime.list_pending_approvals(),
            "audit": context.runtime.list_audit_events(12),
            "notices": [
                "当前为空数据第一阶段环境，不含源销售平台的客户、任务、文件或配置。",
                domain_notice,
                "模型和外部搜索尚未配置；平台可离线查看能力与验证受控流程。",
            ],
        }

    def _profile_catalog(self) -> dict[str, Any]:
        context = self.server.context
        profile = context.profile
        registry_summary = context.registry.public_summary()
        available = set(profile.available_domains)
        enabled = set(profile.enabled_domains)
        domains = []
        for plugin in context.registry.business_domains:
            if plugin.id not in available:
                continue
            domains.append({**plugin.public_summary(), "enabled": plugin.id in enabled})
        return {
            "profile": profile.public_summary(),
            "platform_capabilities": registry_summary["platform_capabilities"],
            "business_domains": domains,
            "workflows": [
                workflow
                for workflow in registry_summary["workflows"]
                if workflow["plugin"] in enabled
            ],
        }

    def _serve_static(self, request_path: str) -> None:
        relative_path = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (self.server.context.ui_root / relative_path).resolve()
        if not candidate.is_relative_to(self.server.context.ui_root) or candidate.is_symlink():
            self._write_error(HTTPStatus.NOT_FOUND, "页面不存在")
            return
        if not candidate.is_file():
            candidate = self.server.context.ui_root / "index.html"
        body = candidate.read_bytes()
        content_type = STATIC_CONTENT_TYPES.get(
            candidate.suffix.lower(), mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        self.send_response(HTTPStatus.OK)
        self._security_headers(content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    *,
    project_root: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8766,
    profile_id: str = "company-manager",
) -> CompanyHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("公司工作台第一阶段只允许绑定本机回环地址")
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    ui_root = (root / "ui").resolve(strict=True)
    registry = load_registry(root)
    profile = load_profile(root, registry, profile_id)
    runtime_root = Path(runtime_dir or root / "runtime").resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)

    def context_factory(actual_port: int) -> ServerContext:
        return ServerContext(
            project_root=root,
            ui_root=ui_root,
            registry=registry,
            profile=profile,
            runtime=RuntimeStore(
                runtime_root / "company-platform.db",
                registry,
                enabled_domains=profile.enabled_domains,
            ),
            session_token=secrets.token_urlsafe(32),
            host=host,
            port=actual_port,
        )

    return CompanyHTTPServer((host, port), context_factory)


def serve(
    *,
    project_root: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = False,
    profile_id: str = "company-manager",
) -> None:
    server = create_server(
        project_root=project_root,
        runtime_dir=runtime_dir,
        host=host,
        port=port,
        profile_id=profile_id,
    )
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(
        json.dumps(
            {
                "status": "ready",
                "url": url,
                "product": "agent4company",
                "profile_id": server.context.profile.id,
            },
            ensure_ascii=False,
        )
    )
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
