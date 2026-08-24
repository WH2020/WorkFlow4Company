from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from company_platform.server import create_server


ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server(
            project_root=ROOT,
            runtime_dir=self.temporary.name,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None, token: str = ""):
        return self.request_at(self.base, path, method=method, payload=payload, token=token)

    def request_at(
        self,
        base: str,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        token: str = "",
    ):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Company-Session"] = token
        request = Request(base + path, data=data, headers=headers, method=method)
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    @staticmethod
    def multipart_payload(
        fields: dict[str, str], filename: str, content: bytes
    ) -> tuple[str, bytes]:
        boundary = "Agent4CompanyLibraryBoundary"
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
        )
        body.extend(content)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        return f"multipart/form-data; boundary={boundary}", bytes(body)

    @staticmethod
    def raw_request_at(
        base: str,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        token: str = "",
    ):
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if token:
            headers["X-Company-Session"] = token
        request = Request(base + path, data=data, headers=headers, method=method)
        with urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()

    def test_health_and_bootstrap_are_company_level(self) -> None:
        status, health = self.request("/api/health")
        self.assertEqual(200, status)
        self.assertEqual("agent4company", health["product_id"])
        self.assertEqual("company-manager", health["profile_id"])
        _, bootstrap = self.request("/api/bootstrap")
        self.assertEqual("公司管理平台", bootstrap["product"]["name"])
        self.assertEqual(8, len(bootstrap["platform_capabilities"]))
        capability_modes = {
            item["id"]: item["configuration_mode"]
            for item in bootstrap["platform_capabilities"]
        }
        self.assertEqual("adapter-ready", capability_modes["platform.files"])
        self.assertEqual("local-empty", capability_modes["platform.knowledge"])
        self.assertEqual("unconfigured", capability_modes["platform.model-gateway"])
        self.assertEqual("built-in", capability_modes["platform.library"])
        self.assertEqual(["domain.sales"], [item["id"] for item in bootstrap["business_domains"]])
        self.assertFalse(bootstrap["business_domains"][0]["enabled"])
        self.assertEqual([], bootstrap["workflows"])
        self.assertEqual([], bootstrap["profile"]["enabled_domains"])
        self.assertEqual({}, bootstrap["library"]["items"])

    def test_company_library_http_flow_is_local_versioned_and_audited(self) -> None:
        _, bootstrap = self.request("/api/bootstrap")
        token = bootstrap["session_token"]
        with self.assertRaises(HTTPError) as no_session:
            self.request("/api/library")
        self.assertEqual(403, no_session.exception.code)

        content_type, body = self.multipart_payload(
            {
                "title": "公司融资 BP",
                "category": "bp",
                "confidentiality": "confidential",
                "tags": "融资,战略",
                "version_note": "第一版",
            },
            "startup-bp.md",
            "# 公司融资 BP\n核心产品用于工业研发协作。".encode(),
        )
        status, _, raw = self.raw_request_at(
            self.base,
            "/api/library/import",
            method="POST",
            data=body,
            content_type=content_type,
            token=token,
        )
        self.assertEqual(201, status)
        imported = json.loads(raw.decode("utf-8"))
        item = imported["item"]
        item_id = item["item_id"]
        first_version_id = item["current_version_id"]
        self.assertEqual("confidential", item["confidentiality"])
        self.assertIn("工业研发协作", item["current_version"]["preview"])

        _, collection = self.request(
            f"/api/library?q={quote('研发协作')}", token=token
        )
        self.assertEqual([item_id], [entry["item_id"] for entry in collection["items"]])
        self.assertIn("研发协作", collection["items"][0]["match"]["snippet"])

        download_status, headers, downloaded = self.raw_request_at(
            self.base,
            f"/api/library/versions/{first_version_id}/content",
            token=token,
        )
        self.assertEqual(200, download_status)
        self.assertEqual(body_content := "# 公司融资 BP\n核心产品用于工业研发协作。".encode(), downloaded)
        self.assertEqual(64, len(headers["X-Content-SHA256"]))

        content_type, body = self.multipart_payload(
            {
                "item_id": item_id,
                "make_current": "true",
                "owner_confirmed": "true",
                "version_note": "更新财务预测",
            },
            "startup-bp-v2.md",
            body_content + "\n新增三年财务预测。".encode(),
        )
        _, _, raw = self.raw_request_at(
            self.base,
            "/api/library/import",
            method="POST",
            data=body,
            content_type=content_type,
            token=token,
        )
        updated = json.loads(raw.decode("utf-8"))["item"]
        self.assertEqual(2, len(updated["versions"]))
        self.assertNotEqual(first_version_id, updated["current_version_id"])
        self.assertEqual("更新财务预测", updated["versions"][0]["version_note"])

        _, switched = self.request(
            f"/api/library/items/{item_id}/current",
            method="POST",
            token=token,
            payload={"version_id": first_version_id, "owner_confirmed": True},
        )
        self.assertEqual(first_version_id, switched["item"]["current_version_id"])

        self.request(
            f"/api/library/items/{item_id}/archive",
            method="POST",
            token=token,
            payload={"owner_confirmed": True},
        )
        _, active = self.request("/api/library", token=token)
        _, archived = self.request("/api/library?status=archived", token=token)
        self.assertEqual([], active["items"])
        self.assertEqual([item_id], [entry["item_id"] for entry in archived["items"]])
        self.request(
            f"/api/library/items/{item_id}/restore",
            method="POST",
            token=token,
            payload={"owner_confirmed": True},
        )
        _, audit = self.request("/api/audit")
        actions = [event["action"] for event in audit["events"]]
        self.assertIn("library.item.imported", actions)
        self.assertIn("library.version.added", actions)
        self.assertIn("library.item.archived", actions)
        self.assertIn("library.item.restored", actions)

    def test_company_library_rejects_disguised_files_and_missing_confirmation(self) -> None:
        _, bootstrap = self.request("/api/bootstrap")
        token = bootstrap["session_token"]
        content_type, body = self.multipart_payload({}, "disguised.pdf", b"not a PDF")
        with self.assertRaises(HTTPError) as rejected:
            self.raw_request_at(
                self.base,
                "/api/library/import",
                method="POST",
                data=body,
                content_type=content_type,
                token=token,
            )
        self.assertEqual(400, rejected.exception.code)

        content_type, body = self.multipart_payload({}, "safe.txt", b"safe content")
        _, _, raw = self.raw_request_at(
            self.base,
            "/api/library/import",
            method="POST",
            data=body,
            content_type=content_type,
            token=token,
        )
        item_id = json.loads(raw.decode("utf-8"))["item"]["item_id"]
        with self.assertRaises(HTTPError) as unconfirmed:
            self.request(
                f"/api/library/items/{item_id}/archive",
                method="POST",
                token=token,
                payload={"owner_confirmed": False},
            )
        self.assertEqual(400, unconfirmed.exception.code)

    def test_company_library_rejects_ambiguous_queries_and_invalid_metadata_types(self) -> None:
        _, bootstrap = self.request("/api/bootstrap")
        token = bootstrap["session_token"]
        with self.assertRaises(HTTPError) as duplicate_query:
            self.request("/api/library?status=current&status=archived", token=token)
        self.assertEqual(400, duplicate_query.exception.code)

        content_type, body = self.multipart_payload({}, "metadata.txt", b"safe content")
        _, _, raw = self.raw_request_at(
            self.base,
            "/api/library/import",
            method="POST",
            data=body,
            content_type=content_type,
            token=token,
        )
        item_id = json.loads(raw.decode("utf-8"))["item"]["item_id"]
        with self.assertRaises(HTTPError) as invalid_type:
            self.request(
                f"/api/library/items/{item_id}/metadata",
                method="POST",
                token=token,
                payload={"title": 123},
            )
        self.assertEqual(400, invalid_type.exception.code)

    def test_company_library_rejects_nested_multipart_upload(self) -> None:
        _, bootstrap = self.request("/api/bootstrap")
        token = bootstrap["session_token"]
        outer = "OuterBoundary"
        inner = "InnerBoundary"
        body = (
            f"--{outer}\r\n"
            'Content-Disposition: form-data; name="file"; filename="nested.txt"\r\n'
            f"Content-Type: multipart/mixed; boundary={inner}\r\n\r\n"
            f"--{inner}\r\nContent-Type: text/plain\r\n\r\nnested\r\n--{inner}--\r\n"
            f"--{outer}--\r\n"
        ).encode()
        with self.assertRaises(HTTPError) as rejected:
            self.raw_request_at(
                self.base,
                "/api/library/import",
                method="POST",
                data=body,
                content_type=f"multipart/form-data; boundary={outer}",
                token=token,
            )
        self.assertEqual(400, rejected.exception.code)

    def test_post_requires_session_and_disabled_domain_is_rejected(self) -> None:
        _, bootstrap = self.request("/api/bootstrap")
        with self.assertRaises(HTTPError) as blocked:
            self.request(
                "/api/tasks",
                method="POST",
                payload={"workflow_id": "domain.sales.pipeline-review", "title": "无令牌任务"},
            )
        self.assertEqual(403, blocked.exception.code)
        with self.assertRaises(HTTPError) as disabled:
            self.request(
                "/api/tasks",
                method="POST",
                token=bootstrap["session_token"],
                payload={"workflow_id": "domain.sales.pipeline-review", "title": "禁用域任务"},
            )
        self.assertEqual(403, disabled.exception.code)

    def test_sales_profile_flow_uses_shared_company_fact_store(self) -> None:
        sales_server = create_server(
            project_root=ROOT,
            runtime_dir=self.temporary.name,
            port=0,
            profile_id="company-with-sales",
        )
        sales_thread = threading.Thread(target=sales_server.serve_forever, daemon=True)
        sales_thread.start()
        sales_base = f"http://127.0.0.1:{sales_server.server_address[1]}"
        try:
            _, sales_bootstrap = self.request_at(sales_base, "/api/bootstrap")
            self.assertEqual(["domain.sales"], sales_bootstrap["profile"]["enabled_domains"])
            status, created = self.request_at(
                sales_base,
                "/api/tasks",
                method="POST",
                token=sales_bootstrap["session_token"],
                payload={"workflow_id": "domain.sales.pipeline-review", "title": "销售域接口验证"},
            )
            self.assertEqual(201, status)
            self.assertEqual("waiting_approval", created["task"]["status"])
            approval_id = created["task"]["approvals"][0]["approval_id"]
            _, approved = self.request_at(
                sales_base,
                f"/api/approvals/{approval_id}/decision",
                method="POST",
                token=sales_bootstrap["session_token"],
                payload={"decision": "approved", "reason": "测试确认"},
            )
            self.assertEqual("completed", approved["task"]["status"])
            _, company_bootstrap = self.request("/api/bootstrap")
            self.assertEqual([created["task"]["task_id"]], [task["task_id"] for task in company_bootstrap["tasks"]])
        finally:
            sales_server.shutdown()
            sales_server.server_close()
            sales_thread.join(timeout=3)

    def test_company_profile_starts_when_sales_plugin_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "plugins/platform", root / "plugins/platform")
            shutil.copytree(ROOT / "profiles/company-manager", root / "profiles/company-manager")
            shutil.copytree(ROOT / "ui", root / "ui")
            server = create_server(project_root=root, runtime_dir=root / "runtime", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, bootstrap = self.request_at(base, "/api/bootstrap")
                self.assertEqual([], bootstrap["business_domains"])
                self.assertEqual([], bootstrap["workflows"])
                self.assertEqual(8, len(bootstrap["platform_capabilities"]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
