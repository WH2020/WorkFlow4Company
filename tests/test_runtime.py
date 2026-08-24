from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from company_platform.plugin_registry import load_registry
from company_platform.runtime import (
    ConflictError,
    PermissionDeniedError,
    RuntimeIdentity,
    RuntimeStore,
)


ROOT = Path(__file__).resolve().parents[1]


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "runtime.db"
        self.store = RuntimeStore(self.database, load_registry(ROOT))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sales_domain_reaches_approval_then_completes(self) -> None:
        task = self.store.create_task("domain.sales.pipeline-review", "空数据销售复盘")
        self.assertEqual("waiting_approval", task["status"])
        self.assertEqual(1, len(task["approvals"]))
        approval = task["approvals"][0]
        self.assertEqual("company-local", approval["company_id"])
        self.assertEqual("domain.sales", approval["domain_id"])
        self.assertEqual("domain_owner_confirms_sales_changes", approval["policy_id"])
        self.assertEqual(64, len(approval["payload_sha256"]))
        self.assertEqual(1, approval["expected_version"])
        completed = self.store.decide_approval(approval["approval_id"], "approved")
        self.assertEqual("completed", completed["status"])
        self.assertTrue(all(node["status"] == "completed" for node in completed["nodes"]))
        actions = [event["action"] for event in self.store.list_audit_events(100)]
        self.assertIn("task.created", actions)
        self.assertIn("approval.requested", actions)
        self.assertIn("approval.decided", actions)
        self.assertIn("task.completed", actions)

    def test_non_approver_cannot_decide(self) -> None:
        task = self.store.create_task("domain.sales.pipeline-review", "权限测试")
        with self.assertRaises(PermissionDeniedError):
            self.store.decide_approval(
                task["approvals"][0]["approval_id"],
                "approved",
                identity=RuntimeIdentity("member-1", "member", "human"),
            )

    def test_tampered_payload_invalidates_approval(self) -> None:
        task = self.store.create_task("domain.sales.pipeline-review", "载荷哈希测试")
        approval_id = task["approvals"][0]["approval_id"]
        connection = sqlite3.connect(self.database)
        try:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()[0]
            )
            payload["title"] = "已被替换"
            connection.execute(
                "UPDATE approvals SET payload_json = ? WHERE approval_id = ?",
                (json.dumps(payload, ensure_ascii=False), approval_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ConflictError, "载荷哈希不一致"):
            self.store.decide_approval(approval_id, "approved")

    def test_tampered_storage_binding_invalidates_approval(self) -> None:
        task = self.store.create_task("domain.sales.pipeline-review", "存储绑定测试")
        approval_id = task["approvals"][0]["approval_id"]
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE approvals SET storage_binding = ? WHERE approval_id = ?",
                ("sqlite://another-company/domain.sales/task-intents", approval_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ConflictError, "存储绑定已变化"):
            self.store.decide_approval(approval_id, "approved")

    def test_rejection_is_terminal_and_audited(self) -> None:
        task = self.store.create_task("domain.sales.pipeline-review", "驳回路径")
        rejected = self.store.decide_approval(
            task["approvals"][0]["approval_id"], "rejected", reason="信息不足"
        )
        self.assertEqual("rejected", rejected["status"])
        event = self.store.list_audit_events(1)[0]
        self.assertEqual("approval.decided", event["action"])
        self.assertEqual("rejected", event["result"])

    def test_disabled_domain_cannot_create_task(self) -> None:
        disabled = RuntimeStore(
            Path(self.temporary.name) / "disabled.db",
            load_registry(ROOT),
            enabled_domains=(),
        )
        with self.assertRaisesRegex(PermissionDeniedError, "当前 Profile 未启用业务域"):
            disabled.create_task("domain.sales.pipeline-review", "不应启动的销售任务")
        self.assertEqual(0, disabled.dashboard()["enabled_domains"])

    def test_storage_binding_uses_runtime_company_scope(self) -> None:
        scoped = RuntimeStore(
            Path(self.temporary.name) / "scoped.db",
            load_registry(ROOT),
            company_id="company-acme",
        )
        task = scoped.create_task("domain.sales.pipeline-review", "公司作用域验证")
        self.assertEqual("company-acme", task["company_id"])
        self.assertEqual(
            "sqlite://company-acme/domain.sales/task-intents",
            task["approvals"][0]["storage_binding"],
        )


if __name__ == "__main__":
    unittest.main()
