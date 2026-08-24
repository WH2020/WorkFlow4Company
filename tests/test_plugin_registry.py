from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from company_platform.plugin_registry import (
    RegistryError,
    load_registry,
    plan_workflow,
    validate_workflow,
)


ROOT = Path(__file__).resolve().parents[1]


class PluginRegistryTests(unittest.TestCase):
    def test_loads_company_capabilities_and_sales_as_domain(self) -> None:
        registry = load_registry(ROOT)
        self.assertEqual(7, len(registry.platform_capabilities))
        self.assertEqual(["domain.sales"], [domain.id for domain in registry.business_domains])
        self.assertEqual(["domain.sales.pipeline-review"], sorted(registry.workflows))
        self.assertTrue(all("sales" not in item.id for item in registry.platform_capabilities))

    def test_sales_workflow_has_deterministic_approval_stage(self) -> None:
        workflow = load_registry(ROOT).workflows["domain.sales.pipeline-review"]
        self.assertEqual(
            [
                ["load_sales_context"],
                ["draft_review"],
                ["owner_approval"],
                ["record_actions"],
                ["verify_audit"],
            ],
            [[node.id for node in stage] for stage in plan_workflow(workflow)],
        )

    def test_structured_write_cannot_bypass_approval(self) -> None:
        registry = load_registry(ROOT)
        workflow = registry.workflows["domain.sales.pipeline-review"]
        manifest = registry.plugins[workflow.plugin]
        unsafe_nodes = tuple(
            replace(node, depends_on=("draft_review",)) if node.id == "record_actions" else node
            for node in workflow.nodes
            if node.id != "owner_approval"
        )
        unsafe = replace(workflow, nodes=unsafe_nodes)
        with self.assertRaisesRegex(RegistryError, "结构化写入必须只有一个直接审批前驱"):
            validate_workflow(unsafe, manifest)

    def test_agent_cannot_claim_structured_write_permission(self) -> None:
        registry = load_registry(ROOT)
        workflow = registry.workflows["domain.sales.pipeline-review"]
        manifest = registry.plugins[workflow.plugin]
        unsafe_nodes = tuple(
            replace(node, permissions=(*node.permissions, "sales.write"))
            if node.id == "draft_review"
            else node
            for node in workflow.nodes
        )
        unsafe = replace(workflow, nodes=unsafe_nodes)
        with self.assertRaisesRegex(RegistryError, "结构化写权限只能声明在 Tool 节点"):
            validate_workflow(unsafe, manifest)

    def test_platform_registry_starts_without_sales_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "plugins/platform", root / "plugins/platform")
            registry = load_registry(root)
            self.assertEqual(7, len(registry.platform_capabilities))
            self.assertEqual([], registry.business_domains)
            self.assertEqual({}, registry.workflows)

    def test_default_company_profile_does_not_enable_sales(self) -> None:
        profile = json.loads(
            (ROOT / "profiles/company-manager/profile.json").read_text(encoding="utf-8")
        )
        self.assertEqual("company-manager", profile["id"])
        self.assertEqual([], profile["enabled_domains"])
        self.assertEqual(["domain.sales"], profile["available_domains"])

    def test_new_domain_declares_tools_without_core_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "plugins/platform", root / "plugins/platform")
            domain = root / "plugins/domains/delivery"
            (domain / "workflows").mkdir(parents=True)
            manifest = {
                "api_version": "company.platform/v1",
                "id": "domain.delivery",
                "version": "1.0.0",
                "kind": "business-domain",
                "display_name": "交付管理",
                "description": "独立测试业务域。",
                "permissions": ["delivery.read"],
                "write_permissions": [],
                "tools": [
                    {"name": "delivery.read", "effect": "read", "permissions": ["delivery.read"]}
                ],
                "dependencies": [],
                "capabilities": ["delivery.review"],
                "skills": [],
                "workflows": ["workflows/review.json"],
            }
            workflow = {
                "id": "domain.delivery.review",
                "plugin": "domain.delivery",
                "display_name": "交付复盘",
                "description": "读取交付事实。",
                "entry_nodes": ["load_context"],
                "output_nodes": ["load_context"],
                "nodes": [
                    {
                        "id": "load_context",
                        "type": "tool",
                        "tool": "delivery.read",
                        "depends_on": [],
                        "permissions": ["delivery.read"],
                    }
                ],
            }
            (domain / "plugin.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            (domain / "workflows/review.json").write_text(
                json.dumps(workflow, ensure_ascii=False), encoding="utf-8"
            )
            registry = load_registry(root)
            self.assertIn("domain.delivery.review", registry.workflows)
            self.assertEqual(
                ("delivery.read",),
                registry.plugins["domain.delivery"].tool_map["delivery.read"].permissions,
            )
            self.assertEqual("read", registry.plugins["domain.delivery"].tool_map["delivery.read"].effect)

    def test_write_effect_requires_approval_for_non_write_permission_names(self) -> None:
        registry = load_registry(ROOT)
        current = registry.workflows["domain.sales.pipeline-review"]
        base_manifest = registry.plugins[current.plugin]
        for permission in ("sales.create", "sales.delete", "sales.mutate"):
            with self.subTest(permission=permission):
                write_tool = replace(
                    base_manifest.tool_map["sales.write"],
                    name=permission,
                    effect="write",
                    permissions=(permission,),
                )
                manifest = replace(
                    base_manifest,
                    permissions=tuple(
                        permission if item == "sales.write" else item
                        for item in base_manifest.permissions
                    ),
                    write_permissions=(permission,),
                    tools=tuple(
                        write_tool if tool.name == "sales.write" else tool
                        for tool in base_manifest.tools
                    ),
                )
                unsafe_nodes = tuple(
                    replace(
                        node,
                        tool=permission,
                        permissions=(permission,),
                        depends_on=("draft_review",),
                    )
                    if node.id == "record_actions"
                    else node
                    for node in current.nodes
                    if node.id != "owner_approval"
                )
                unsafe = replace(current, nodes=unsafe_nodes)
                with self.assertRaisesRegex(
                    RegistryError, "结构化写入必须只有一个直接审批前驱"
                ):
                    validate_workflow(unsafe, manifest)

    def test_read_permission_on_write_tool_can_be_used_by_agent(self) -> None:
        registry = load_registry(ROOT)
        presentation = registry.plugins["platform.presentation"]
        self.assertEqual(
            ("knowledge.read", "presentation.plan.write"),
            presentation.tool_map["presentation.plan"].permissions,
        )
        self.assertEqual(
            ("presentation.plan.write", "artifact.write"),
            presentation.write_permissions,
        )
        manifest = replace(presentation, skills=("read-presentation-context",))
        base = registry.workflows["domain.sales.pipeline-review"]
        workflow = replace(
            base,
            id="platform.presentation.read-context",
            plugin=manifest.id,
            entry_nodes=("read_context",),
            output_nodes=("read_context",),
            nodes=(
                replace(
                    base.node_map["draft_review"],
                    id="read_context",
                    depends_on=(),
                    permissions=("knowledge.read",),
                    skill="read-presentation-context",
                ),
            ),
        )
        validate_workflow(workflow, manifest)

    def test_platform_core_has_no_sales_tool_special_case(self) -> None:
        for relative in (
            "company_platform/plugin_registry.py",
            "pi/extensions/platform-core.ts",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn('"sales.read"', source)
            self.assertNotIn('"sales.write"', source)
        for relative in (
            "company_platform/cli.py",
            "company_platform/server.py",
            "desktop/src-tauri/src/main.rs",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn('"domain.sales"', source)
            self.assertNotIn("manage-sales", source)
            self.assertNotIn('"company-with-sales"', source)


if __name__ == "__main__":
    unittest.main()
