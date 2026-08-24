from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual("1.0.0", approval["plugin_version"])
        self.assertEqual(64, len(approval["workflow_fingerprint"]))
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

    def test_restart_recovers_task_committed_before_initial_advance(self) -> None:
        database = Path(self.temporary.name) / "recover-create.db"
        interrupted = RuntimeStore(database, load_registry(ROOT))
        with patch.object(interrupted, "_advance", side_effect=RuntimeError("模拟进程中断")):
            with self.assertRaisesRegex(RuntimeError, "模拟进程中断"):
                interrupted.create_task("domain.sales.pipeline-review", "创建后中断")
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(
                "running",
                connection.execute("SELECT status FROM tasks").fetchone()[0],
            )
        finally:
            connection.close()
        recovered = RuntimeStore(database, load_registry(ROOT), enabled_domains=())
        task = recovered.list_tasks(1)[0]
        self.assertEqual("waiting_approval", task["status"])
        self.assertEqual(1, len(recovered.get_task(task["task_id"])["approvals"]))

    def test_restart_recovers_approved_task_without_replaying_nodes(self) -> None:
        task = self.store.create_task("domain.sales.pipeline-review", "批准后中断")
        approval_id = task["approvals"][0]["approval_id"]
        with patch.object(self.store, "_advance", side_effect=RuntimeError("模拟进程中断")):
            with self.assertRaisesRegex(RuntimeError, "模拟进程中断"):
                self.store.decide_approval(approval_id, "approved")
        recovered = RuntimeStore(self.database, load_registry(ROOT), enabled_domains=())
        completed = recovered.get_task(task["task_id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("approved", completed["approvals"][0]["decision"])
        completed_events = [
            event["node_id"]
            for event in recovered.list_audit_events(100)
            if event["action"] == "node.completed"
        ]
        self.assertEqual(len(completed_events), len(set(completed_events)))

    def test_restart_refuses_approval_after_workflow_contract_changes(self) -> None:
        database = Path(self.temporary.name) / "recover-upgrade.db"
        registry = load_registry(ROOT)
        interrupted = RuntimeStore(database, registry)
        task = interrupted.create_task("domain.sales.pipeline-review", "批准后升级")
        approval_id = task["approvals"][0]["approval_id"]
        with patch.object(interrupted, "_advance", side_effect=RuntimeError("模拟进程中断")):
            with self.assertRaisesRegex(RuntimeError, "模拟进程中断"):
                interrupted.decide_approval(approval_id, "approved")

        workflow = registry.workflows["domain.sales.pipeline-review"]
        changed_workflow = replace(workflow, display_name="已升级的销售复盘语义")
        changed_registry = replace(
            registry,
            workflows={**registry.workflows, workflow.id: changed_workflow},
        )
        recovered = RuntimeStore(database, changed_registry)
        failed = recovered.get_task(task["task_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("approved", failed["approvals"][0]["decision"])
        self.assertNotEqual(
            failed["workflow_fingerprint"],
            recovered._workflow_fingerprint(changed_workflow),
        )
        write_node = next(node for node in failed["nodes"] if node["node_id"] == "record_actions")
        self.assertNotEqual("completed", write_node["status"])
        invalidations = [
            event
            for event in recovered.list_audit_events(100)
            if event["action"] == "runtime.contract_invalidated"
        ]
        self.assertEqual(1, len(invalidations))

    def test_pending_approval_is_invalidated_after_workflow_contract_changes(self) -> None:
        database = Path(self.temporary.name) / "pending-upgrade.db"
        registry = load_registry(ROOT)
        original = RuntimeStore(database, registry)
        task = original.create_task("domain.sales.pipeline-review", "待审批时升级")
        workflow = registry.workflows["domain.sales.pipeline-review"]
        changed_workflow = replace(workflow, display_name="已升级的待审批销售复盘")
        changed_registry = replace(
            registry,
            workflows={**registry.workflows, workflow.id: changed_workflow},
        )
        upgraded = RuntimeStore(database, changed_registry)
        failed = upgraded.get_task(task["task_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("invalidated", failed["approvals"][0]["decision"])
        approval_node = next(
            node for node in failed["nodes"] if node["node_id"] == "owner_approval"
        )
        self.assertEqual("failed", approval_node["status"])
        self.assertEqual(
            1,
            sum(
                event["action"] == "runtime.contract_invalidated"
                for event in upgraded.list_audit_events(100)
            ),
        )


if __name__ == "__main__":
    unittest.main()
