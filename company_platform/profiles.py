from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .plugin_registry import PluginRegistry, RegistryError


SAFE_PROFILE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
PROFILE_KEYS = {
    "id",
    "display_name",
    "description",
    "enabled_domains",
    "available_domains",
    "default_view",
    "default_workflow",
    "roles",
}


class ProfileError(RegistryError):
    """公司工作台组合 Profile 不满足安装与启用边界。"""


@dataclass(frozen=True)
class CompanyProfile:
    id: str
    display_name: str
    description: str
    enabled_domains: tuple[str, ...]
    available_domains: tuple[str, ...]
    default_view: str
    default_workflow: str | None
    roles: tuple[str, ...]
    source_path: Path

    def public_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "enabled_domains": list(self.enabled_domains),
            "available_domains": list(self.available_domains),
            "default_view": self.default_view,
            "default_workflow": self.default_workflow,
            "roles": list(self.roles),
        }


def _required_string(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ProfileError(f"{label} 必须是 1-{max_length} 字的非空字符串")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProfileError(f"{label} 必须是字符串数组")
    if len(set(value)) != len(value):
        raise ProfileError(f"{label} 不能包含重复值")
    return tuple(value)


def load_profile(
    project_root: Path | str,
    registry: PluginRegistry,
    profile_id: str = "company-manager",
) -> CompanyProfile:
    if not SAFE_PROFILE_ID.fullmatch(profile_id):
        raise ProfileError(f"Profile ID 无效：{profile_id}")
    root = Path(project_root).resolve(strict=True)
    profiles_root = (root / "profiles").resolve(strict=True)
    source_path = (profiles_root / profile_id / "profile.json").resolve(strict=True)
    if not source_path.is_relative_to(profiles_root) or source_path.is_symlink():
        raise ProfileError(f"Profile 路径越界或为符号链接：{profile_id}")
    if not source_path.is_file() or source_path.stat().st_size > 128 * 1024:
        raise ProfileError(f"Profile 不是受限普通文件：{profile_id}")
    try:
        value = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"Profile JSON 无效：{profile_id}") from error
    if not isinstance(value, dict):
        raise ProfileError(f"Profile 必须是对象：{profile_id}")
    unknown = sorted(set(value) - PROFILE_KEYS)
    missing = sorted(PROFILE_KEYS - set(value))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"未知字段 {', '.join(unknown)}")
        if missing:
            details.append(f"缺少字段 {', '.join(missing)}")
        raise ProfileError(f"Profile {profile_id} 字段无效：{'；'.join(details)}")

    parsed_id = _required_string(value["id"], "Profile ID", 80)
    if parsed_id != profile_id:
        raise ProfileError(f"Profile 目录与 ID 不一致：{profile_id}/{parsed_id}")
    enabled_domains = _string_tuple(value["enabled_domains"], f"Profile {profile_id} 已启用业务域")
    available_domains = _string_tuple(
        value["available_domains"], f"Profile {profile_id} 可用业务域"
    )
    if not set(enabled_domains).issubset(available_domains):
        raise ProfileError(f"Profile {profile_id} 启用的业务域必须先声明为可用")
    installed_domains = {plugin.id for plugin in registry.business_domains}
    unavailable_enabled = sorted(set(enabled_domains) - installed_domains)
    if unavailable_enabled:
        raise ProfileError(
            f"Profile {profile_id} 启用了未安装业务域：{', '.join(unavailable_enabled)}"
        )

    default_workflow = value["default_workflow"]
    if default_workflow is not None:
        default_workflow = _required_string(default_workflow, "默认工作流", 180)
        workflow = registry.workflows.get(default_workflow)
        if workflow is None:
            raise ProfileError(f"Profile {profile_id} 默认工作流未安装：{default_workflow}")
        if workflow.plugin not in enabled_domains:
            raise ProfileError(f"Profile {profile_id} 默认工作流所属业务域未启用")

    return CompanyProfile(
        id=parsed_id,
        display_name=_required_string(value["display_name"], "Profile 显示名称", 100),
        description=_required_string(value["description"], "Profile 描述", 500),
        enabled_domains=enabled_domains,
        available_domains=available_domains,
        default_view=_required_string(value["default_view"], "Profile 默认视图", 100),
        default_workflow=default_workflow,
        roles=_string_tuple(value["roles"], f"Profile {profile_id} 角色"),
        source_path=source_path,
    )
