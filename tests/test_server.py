from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
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

    def test_health_and_bootstrap_are_company_level(self) -> None:
        status, health = self.request("/api/health")
        self.assertEqual(200, status)
        self.assertEqual("agent4company", health["product_id"])
        self.assertEqual("company-manager", health["profile_id"])
        _, bootstrap = self.request("/api/bootstrap")
        self.assertEqual("公司管理平台", bootstrap["product"]["name"])
        self.assertEqual(7, len(bootstrap["platform_capabilities"]))
        capability_modes = {
            item["id"]: item["configuration_mode"]
            for item in bootstrap["platform_capabilities"]
        }
        self.assertEqual("adapter-ready", capability_modes["platform.files"])
        self.assertEqual("local-empty", capability_modes["platform.knowledge"])
        self.assertEqual("unconfigured", capability_modes["platform.model-gateway"])
        self.assertEqual(["domain.sales"], [item["id"] for item in bootstrap["business_domains"]])
        self.assertFalse(bootstrap["business_domains"][0]["enabled"])
        self.assertEqual([], bootstrap["workflows"])
        self.assertEqual([], bootstrap["profile"]["enabled_domains"])

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
                self.assertEqual(7, len(bootstrap["platform_capabilities"]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
