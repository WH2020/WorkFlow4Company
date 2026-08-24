from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from company_platform.cli import command_launch, command_validate
from company_platform.plugin_registry import load_registry
from company_platform.profiles import ProfileError, load_profile, resolve_profile_skill_directories


ROOT = Path(__file__).resolve().parents[1]


def create_delivery_project(root: Path) -> None:
    shutil.copytree(ROOT / "plugins/platform", root / "plugins/platform")
    domain = root / "plugins/domains/delivery"
    domain.mkdir(parents=True)
    manifest = {
        "api_version": "company.platform/v1",
        "id": "domain.delivery",
        "version": "1.0.0",
        "kind": "business-domain",
        "display_name": "交付管理",
        "description": "用于验证业务域 Skill 的通用贡献机制。",
        "permissions": ["delivery.read"],
        "write_permissions": [],
        "tools": [
            {"name": "delivery.read", "effect": "read", "permissions": ["delivery.read"]}
        ],
        "dependencies": [],
        "capabilities": ["delivery.review"],
        "skills": ["manage-delivery-domain"],
        "workflows": [],
    }
    (domain / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    profile_dir = root / "profiles/company-with-delivery"
    profile_dir.mkdir(parents=True)
    profile = {
        "id": "company-with-delivery",
        "display_name": "公司管理平台 · 交付域验证",
        "description": "合成验证 Profile。",
        "enabled_domains": ["domain.delivery"],
        "available_domains": ["domain.delivery"],
        "default_view": "company-overview",
        "default_workflow": None,
        "roles": ["company-admin"],
    }
    (profile_dir / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copytree(ROOT / "pi/skills/manage-company", root / "pi/skills/manage-company")
    delivery_skill = root / "pi/skills/manage-delivery-domain"
    delivery_skill.mkdir(parents=True)
    (delivery_skill / "SKILL.md").write_text(
        "---\nname: manage-delivery-domain\ndescription: 合成交付域。\n---\n\n# 交付域\n",
        encoding="utf-8",
    )


class ProfileSkillTests(unittest.TestCase):
    def test_validate_discovers_a_third_profile_without_core_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "plugins", root / "plugins")
            shutil.copytree(ROOT / "profiles", root / "profiles")
            shutil.copytree(ROOT / "pi/skills", root / "pi/skills")
            observer = root / "profiles/company-observer"
            observer.mkdir()
            (observer / "profile.json").write_text(
                json.dumps(
                    {
                        "id": "company-observer",
                        "display_name": "公司管理平台 · 观察组合",
                        "description": "不启用业务域的合成验证 Profile。",
                        "enabled_domains": [],
                        "available_domains": ["domain.sales"],
                        "default_view": "company-overview",
                        "default_workflow": None,
                        "roles": ["auditor"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with patch("company_platform.cli.project_root", return_value=root):
                with redirect_stdout(output):
                    self.assertEqual(0, command_validate())
            self.assertEqual(3, json.loads(output.getvalue())["profiles"])

    def test_real_profiles_resolve_only_enabled_domain_skills(self) -> None:
        registry = load_registry(ROOT)
        company = load_profile(ROOT, registry, "company-manager")
        sales = load_profile(ROOT, registry, "company-with-sales")
        self.assertEqual(
            ["manage-company"],
            [path.name for path in resolve_profile_skill_directories(ROOT, registry, company)],
        )
        self.assertEqual(
            ["manage-company", "manage-sales-domain"],
            [path.name for path in resolve_profile_skill_directories(ROOT, registry, sales)],
        )

    def test_delivery_domain_contributes_skill_without_cli_special_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_delivery_project(root)
            command = root / "node_modules/.bin/pi.CMD"
            command.parent.mkdir(parents=True)
            command.write_text("@echo off\r\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0)
            with (
                patch("company_platform.cli.project_root", return_value=root),
                patch("company_platform.cli.subprocess.run", return_value=completed) as run,
            ):
                self.assertEqual(0, command_launch([], "company-with-delivery"))
            arguments = run.call_args.args[0]
            self.assertIn(str(root / "pi/skills/manage-company"), arguments)
            self.assertIn(str(root / "pi/skills/manage-delivery-domain"), arguments)
            self.assertNotIn("manage-sales-domain", " ".join(arguments))
            self.assertEqual(
                "company-with-delivery",
                run.call_args.kwargs["env"]["AGENT4COMPANY_PROFILE"],
            )

    def test_skill_frontmatter_name_must_match_plugin_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_delivery_project(root)
            skill_file = root / "pi/skills/manage-delivery-domain/SKILL.md"
            skill_file.write_text(
                "---\nname: another-skill\ndescription: 不一致的合成技能。\n---\n",
                encoding="utf-8",
            )
            registry = load_registry(root)
            profile = load_profile(root, registry, "company-with-delivery")
            with self.assertRaisesRegex(ProfileError, "frontmatter 不一致"):
                resolve_profile_skill_directories(root, registry, profile)


if __name__ == "__main__":
    unittest.main()
