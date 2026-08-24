from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as default_email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .library import (
    BlobIntegrityError,
    LibraryConflictError,
    LibraryNotFoundError,
    LibraryStore,
    LibraryValidationError,
    OwnerConfirmationRequired,
)
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
MAX_LIBRARY_FILE_BYTES = 50 * 1024 * 1024
MAX_LIBRARY_UPLOAD_BYTES = MAX_LIBRARY_FILE_BYTES + 128 * 1024
MAX_LIBRARY_PREVIEW_CHARACTERS = 100_000
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
    library: LibraryStore
    session_token: str
    host: str
    port: int


class CompanyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], context_factory: Any) -> None:
        self.library_upload_lock = threading.Lock()
        super().__init__(address, CompanyRequestHandler)
        self.context = context_factory(self.server_address[1])


class CompanyRequestHandler(BaseHTTPRequestHandler):
    server: CompanyHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30.0)

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

    def _read_library_upload(self) -> tuple[dict[str, str], str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if "\r" in content_type or "\n" in content_type or not content_type.lower().startswith(
            "multipart/form-data;"
        ):
            raise ValueError("资料导入必须使用 multipart/form-data")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ValueError("资料导入缺少有效 Content-Length")
        length = int(raw_length)
        if length < 1 or length > MAX_LIBRARY_UPLOAD_BYTES:
            raise ValueError("资料导入请求大小超出 50 MB 限制")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("资料导入内容不完整")
        message = BytesParser(policy=default_email_policy).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii", errors="strict")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + raw
        )
        boundary = message.get_boundary()
        if not message.is_multipart() or boundary is None or not 1 <= len(boundary) <= 70:
            raise ValueError("资料导入边界无效")
        fields: dict[str, str] = {}
        filename: str | None = None
        file_content: bytes | None = None
        parts = list(message.iter_parts())
        if not 1 <= len(parts) <= 20:
            raise ValueError("资料导入字段数量无效")
        for part in parts:
            if part.is_multipart():
                raise ValueError("资料导入不接受嵌套 multipart 内容")
            if part.get_content_disposition() != "form-data":
                raise ValueError("资料导入包含未知内容段")
            name = part.get_param("name", header="content-disposition")
            if not isinstance(name, str) or not name or name in fields:
                raise ValueError("资料导入字段名称无效或重复")
            payload = part.get_payload(decode=True) or b""
            part_filename = part.get_filename()
            if part_filename is not None:
                if name != "file" or filename is not None:
                    raise ValueError("资料导入必须且只能包含一个文件")
                if len(payload) > MAX_LIBRARY_FILE_BYTES:
                    raise ValueError("单个资料文件不能超过 50 MB")
                filename = str(part_filename)
                file_content = payload
                continue
            if len(payload) > 8 * 1024:
                raise ValueError("资料导入文本字段过大")
            try:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8")
            except (LookupError, UnicodeDecodeError) as error:
                raise ValueError("资料导入文本字段必须使用 UTF-8") from error
        if filename is None or file_content is None:
            raise ValueError("请选择要导入的资料文件")
        return fields, filename, file_content

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
        # The request body has not been consumed. Closing this HTTP/1.1
        # connection prevents remaining bytes from being parsed as a new request.
        self.close_connection = True
        self._write_error(HTTPStatus.BAD_REQUEST, "仅接受本机工作台请求")
        return False

    def _require_session(self) -> bool:
        if not self._require_local_request():
            return False
        if not secrets.compare_digest(
            self.headers.get("X-Company-Session", ""), self.server.context.session_token
        ):
            self.close_connection = True
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
            if parsed.path == "/api/library":
                if not self._require_session():
                    return
                self._write_json(self._library_collection(parsed.query))
                return
            if parsed.path.startswith("/api/library/items/"):
                if not self._require_session():
                    return
                item_id = parsed.path.removeprefix("/api/library/items/")
                if not item_id or "/" in item_id:
                    raise LibraryNotFoundError("资料不存在")
                self._write_json({"item": self._library_detail(item_id)})
                return
            if parsed.path.startswith("/api/library/versions/") and parsed.path.endswith("/content"):
                if not self._require_session():
                    return
                version_id = parsed.path.removeprefix("/api/library/versions/").removesuffix(
                    "/content"
                )
                if not version_id or "/" in version_id:
                    raise LibraryNotFoundError("资料版本不存在")
                self._write_library_content(version_id)
                return
            if parsed.path == "/api/plugins":
                self._write_json(self._profile_catalog())
                return
            self._serve_static(parsed.path)
        except NotFoundError as error:
            self._write_error(HTTPStatus.NOT_FOUND, str(error))
        except LibraryNotFoundError as error:
            self._write_error(HTTPStatus.NOT_FOUND, str(error))
        except (LibraryConflictError, BlobIntegrityError) as error:
            self._write_error(HTTPStatus.CONFLICT, str(error))
        except (LibraryValidationError, OwnerConfirmationRequired) as error:
            self._write_error(HTTPStatus.BAD_REQUEST, str(error))
        except (ValueError, ConflictError) as error:
            self._write_error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:  # defensive HTTP boundary
            self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"工作台处理失败：{error}")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._require_session():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/library/import":
                if not self.server.library_upload_lock.acquire(blocking=False):
                    self.close_connection = True
                    self._write_error(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "已有资料正在导入，请稍后再试",
                    )
                    return
                try:
                    fields, filename, content = self._read_library_upload()
                    self._write_json(
                        self._import_library_document(fields, filename, content),
                        HTTPStatus.CREATED,
                    )
                finally:
                    self.server.library_upload_lock.release()
                return
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
            if parsed.path.startswith("/api/library/items/"):
                relative = parsed.path.removeprefix("/api/library/items/")
                if "/" not in relative:
                    self._write_error(HTTPStatus.NOT_FOUND, "接口不存在")
                    return
                item_id, action = relative.rsplit("/", 1)
                if not item_id or "/" in item_id:
                    raise LibraryNotFoundError("资料不存在")
                if action == "current":
                    if set(payload) != {"version_id", "owner_confirmed"}:
                        raise ValueError("版本切换请求字段无效")
                    item = self.server.context.library.set_current_version(
                        item_id,
                        str(payload["version_id"]),
                        owner_confirmed=payload["owner_confirmed"] is True,
                    )
                elif action in {"archive", "restore"}:
                    if set(payload) != {"owner_confirmed"} or payload["owner_confirmed"] is not True:
                        raise OwnerConfirmationRequired("该操作需要本人明确确认")
                    item = (
                        self.server.context.library.archive_item(item_id, owner_confirmed=True)
                        if action == "archive"
                        else self.server.context.library.restore_item(item_id, owner_confirmed=True)
                    )
                elif action == "metadata":
                    allowed = {
                        "title",
                        "category",
                        "confidentiality",
                        "tags",
                        "description",
                        "owner_confirmed",
                    }
                    if set(payload) - allowed:
                        raise ValueError("资料信息请求包含未知字段")
                    for field in ("title", "category", "confidentiality", "description"):
                        if field in payload and payload[field] is not None and not isinstance(
                            payload[field], str
                        ):
                            raise ValueError(f"{field} 必须是字符串")
                    if "owner_confirmed" in payload and not isinstance(
                        payload["owner_confirmed"], bool
                    ):
                        raise ValueError("owner_confirmed 必须是布尔值")
                    tags = payload.get("tags")
                    if tags is not None and (
                        not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)
                    ):
                        raise ValueError("标签必须是字符串数组")
                    item = self.server.context.library.update_item(
                        item_id,
                        title=payload.get("title"),
                        category=payload.get("category"),
                        confidentiality=payload.get("confidentiality"),
                        tags=tags,
                        description=payload.get("description"),
                        owner_confirmed=payload.get("owner_confirmed") is True,
                    )
                else:
                    self._write_error(HTTPStatus.NOT_FOUND, "接口不存在")
                    return
                self._write_json({"status": "ok", "item": self._library_detail(item["item_id"])})
                return
            self._write_error(HTTPStatus.NOT_FOUND, "接口不存在")
        except NotFoundError as error:
            self._write_error(HTTPStatus.NOT_FOUND, str(error))
        except PermissionDeniedError as error:
            self._write_error(HTTPStatus.FORBIDDEN, str(error))
        except LibraryNotFoundError as error:
            self._write_error(HTTPStatus.NOT_FOUND, str(error))
        except (LibraryConflictError, BlobIntegrityError) as error:
            self._write_error(HTTPStatus.CONFLICT, str(error))
        except (LibraryValidationError, OwnerConfirmationRequired) as error:
            self._write_error(HTTPStatus.BAD_REQUEST, str(error))
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
        enabled_names = [registry.plugins[domain_id].display_name for domain_id in profile.enabled_domains]
        installed_names = [
            registry.plugins[domain_id].display_name
            for domain_id in profile.available_domains
            if domain_id in registry.plugins
        ]
        if enabled_names:
            domain_notice = (
                f"当前组合已启用业务域：{'、'.join(enabled_names)}；"
                "可用空数据流程验证审批和审计。"
            )
        elif installed_names:
            domain_notice = (
                f"已安装候选业务域：{'、'.join(installed_names)}；"
                "默认组合未启用业务流程，公司核心可独立运行。"
            )
        else:
            domain_notice = "当前未安装业务域；公司核心与共享能力仍可独立运行。"
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
            "library": context.library.statistics(),
            "navigation": [
                {"id": "overview", "label": "公司总览", "icon": "home"},
                {"id": "tasks", "label": "任务中心", "icon": "tasks"},
                {"id": "approvals", "label": "审批中心", "icon": "approval"},
                {"id": "projects", "label": "项目空间", "icon": "projects"},
                {"id": "knowledge", "label": "公司资料库", "icon": "knowledge"},
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
                "公司资料库已可在本机导入和检索；模型与外部搜索仍未配置。",
            ],
        }

    @staticmethod
    def _parse_library_tags(value: str) -> list[str]:
        normalized = value.replace("，", ",").replace("\n", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def _import_library_document(
        self,
        fields: dict[str, str],
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        allowed = {
            "file",
            "item_id",
            "title",
            "category",
            "confidentiality",
            "tags",
            "description",
            "version_note",
            "make_current",
            "owner_confirmed",
        }
        if set(fields) - allowed:
            raise ValueError("资料导入包含未知字段")
        item_id = fields.get("item_id", "").strip()
        if item_id:
            version = self.server.context.library.add_version(
                item_id,
                filename,
                content,
                make_current=fields.get("make_current") == "true",
                owner_confirmed=fields.get("owner_confirmed") == "true",
                version_note=fields.get("version_note"),
            )
            version.pop("extracted_text", None)
            item = self._library_detail(item_id)
            return {"status": "ok", "item": item, "version": version}
        item = self.server.context.library.import_document(
            filename,
            content,
            title=fields.get("title"),
            category=fields.get("category", "general"),
            confidentiality=fields.get("confidentiality") or None,
            tags=self._parse_library_tags(fields.get("tags", "")),
            description=fields.get("description"),
            version_note=fields.get("version_note"),
        )
        return {"status": "ok", "item": self._library_detail(item["item_id"])}

    @staticmethod
    def _library_item_summary(
        item: dict[str, Any], match: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            key: item[key]
            for key in (
                "item_id",
                "title",
                "category",
                "confidentiality",
                "tags",
                "status",
                "current_version_id",
                "created_at",
                "updated_at",
            )
        } | {
            "version_count": len(item["versions"]),
            "current_version": item["current_version"],
            "match": match,
        }

    def _library_collection(self, raw_query: str) -> dict[str, Any]:
        query = parse_qs(raw_query, keep_blank_values=True)
        if set(query) - {"q", "status", "category", "sort"}:
            raise ValueError("资料列表包含未知查询条件")
        if any(len(values) != 1 for values in query.values()):
            raise ValueError("资料列表查询条件不能重复")
        search_text = query.get("q", [""])[0].strip()
        status = query.get("status", ["current"])[0]
        category = query.get("category", [""])[0] or None
        sort = query.get("sort", ["updated"])[0]
        if status not in {"current", "archived"}:
            raise ValueError("资料状态无效")
        if sort not in {"updated", "title"}:
            raise ValueError("资料排序方式无效")
        if search_text:
            matches = self.server.context.library.search(
                search_text,
                include_archived=status == "archived",
                limit=100,
            )
            if category:
                matches = [match for match in matches if match["category"] == category]
            detailed = [self.server.context.library.get_item(match["item_id"]) for match in matches]
            if status == "archived":
                pairs = [
                    (item, match)
                    for item, match in zip(detailed, matches, strict=True)
                    if item["status"] == "archived"
                ]
            else:
                pairs = list(zip(detailed, matches, strict=True))
        else:
            detailed = self.server.context.library.list_items(
                status=status,
                category=category,
                limit=200,
            )
            pairs = [(self.server.context.library.get_item(item["item_id"]), None) for item in detailed]
        if sort == "title":
            pairs.sort(key=lambda pair: pair[0]["title"].casefold())
        return {
            "items": [self._library_item_summary(item, match) for item, match in pairs],
            "statistics": self.server.context.library.statistics(),
            "query": {"q": search_text, "status": status, "category": category, "sort": sort},
        }

    def _library_detail(self, item_id: str) -> dict[str, Any]:
        item = self.server.context.library.get_item(item_id)
        current = self.server.context.library.get_version(
            item["current_version_id"], include_text=True
        )
        extracted = current.pop("extracted_text", "")
        current["preview"] = extracted[:MAX_LIBRARY_PREVIEW_CHARACTERS]
        current["preview_truncated"] = len(extracted) > MAX_LIBRARY_PREVIEW_CHARACTERS
        item["current_version"] = current
        return item

    def _write_library_content(self, version_id: str) -> None:
        version = self.server.context.library.get_version(version_id)
        content = self.server.context.library.read_content(version_id)
        ascii_name = "document" + version["extension"]
        encoded_name = quote(version["original_filename"], safe="")
        self.send_response(HTTPStatus.OK)
        self._security_headers(version["media_type"] or "application/octet-stream")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
        )
        self.send_header("X-Content-SHA256", version["content_sha256"])
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

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
        database_path = runtime_root / "company-platform.db"
        runtime = RuntimeStore(
            database_path,
            registry,
            enabled_domains=profile.enabled_domains,
        )
        return ServerContext(
            project_root=root,
            ui_root=ui_root,
            registry=registry,
            profile=profile,
            runtime=runtime,
            library=LibraryStore(
                database_path,
                runtime_root / "library",
                company_id=runtime.company_id,
                max_file_bytes=MAX_LIBRARY_FILE_BYTES,
                audit_callback=runtime.record_platform_audit,
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
