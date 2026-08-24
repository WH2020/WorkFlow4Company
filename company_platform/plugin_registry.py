from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SAFE_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
SAFE_NODE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
NODE_TYPES = {"agent", "tool", "subagent", "approval", "parallel", "join", "validator"}


class RegistryError(ValueError):
    """插件或工作流契约不满足平台边界。"""


@dataclass(frozen=True)
class PluginDependency:
    id: str
    version: str


@dataclass(frozen=True)
class PluginTool:
    name: str
    effect: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class PluginManifest:
    api_version: str
    id: str
    version: str
    kind: str
    display_name: str
    description: str
    permissions: tuple[str, ...]
    write_permissions: tuple[str, ...]
    tools: tuple[PluginTool, ...]
    dependencies: tuple[PluginDependency, ...]
    capabilities: tuple[str, ...]
    skills: tuple[str, ...]
    workflows: tuple[str, ...]
    configuration: dict[str, Any]
    navigation: dict[str, Any]
    data_scope: dict[str, Any]
    source_path: Path

    @property
    def tool_map(self) -> dict[str, PluginTool]:
        return {tool.name: tool for tool in self.tools}

    def public_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "kind": self.kind,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "requires_user_configuration": bool(
                self.configuration.get("requires_user_configuration", False)
            ),
            "configuration_mode": self.configuration.get("mode", "built-in"),
            "navigation": self.navigation,
        }


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    type: str
    depends_on: tuple[str, ...]
    permissions: tuple[str, ...]
    tool: str | None = None
    skill: str | None = None
    policy: str | None = None
    check: str | None = None
    boundary: dict[str, Any] | None = None


@dataclass(frozen=True)
class Workflow:
    id: str
    plugin: str
    display_name: str
    description: str
    entry_nodes: tuple[str, ...]
    output_nodes: tuple[str, ...]
    nodes: tuple[WorkflowNode, ...]
    source_path: Path

    @property
    def node_map(self) -> dict[str, WorkflowNode]:
        return {node.id: node for node in self.nodes}


@dataclass(frozen=True)
class PluginRegistry:
    project_root: Path
    plugins: dict[str, PluginManifest]
    workflows: dict[str, Workflow]

    @property
    def platform_capabilities(self) -> list[PluginManifest]:
        return sorted(
            (plugin for plugin in self.plugins.values() if plugin.kind == "platform-capability"),
            key=lambda plugin: plugin.id,
        )

    @property
    def business_domains(self) -> list[PluginManifest]:
        return sorted(
            (plugin for plugin in self.plugins.values() if plugin.kind == "business-domain"),
            key=lambda plugin: plugin.id,
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "platform_capabilities": [plugin.public_summary() for plugin in self.platform_capabilities],
            "business_domains": [plugin.public_summary() for plugin in self.business_domains],
            "workflows": [
                {
                    "id": workflow.id,
                    "plugin": workflow.plugin,
                    "display_name": workflow.display_name,
                    "description": workflow.description,
                    "stages": [[node.id for node in stage] for stage in plan_workflow(workflow)],
                }
                for workflow in sorted(self.workflows.values(), key=lambda item: item.id)
            ],
        }


def _nonempty_string(value: Any, label: str, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise RegistryError(f"{label} 必须是 1-{max_length} 字的非空字符串")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RegistryError(f"{label} 必须是字符串数组")
    if len(set(value)) != len(value):
        raise RegistryError(f"{label} 不能包含重复值")
    return tuple(value)


def _read_json(path: Path, root: Path) -> Any:
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_root):
        raise RegistryError(f"插件路径越界：{path}")
    if path.is_symlink() or not resolved_path.is_file() or resolved_path.stat().st_size > 1024 * 1024:
        raise RegistryError(f"插件 JSON 不是受限普通文件：{path}")
    try:
        return json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryError(f"插件 JSON 无法读取：{path}: {error}") from error


def _parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise RegistryError(f"不支持的语义版本：{value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def satisfies_version(version: str, requirement: str) -> bool:
    actual = _parse_version(version)
    if requirement.startswith(">="):
        return actual >= _parse_version(requirement[2:])
    if requirement.startswith("^"):
        desired = _parse_version(requirement[1:])
        if desired[0] > 0:
            upper = (desired[0] + 1, 0, 0)
        elif desired[1] > 0:
            upper = (0, desired[1] + 1, 0)
        else:
            upper = (0, 0, desired[2] + 1)
        return desired <= actual < upper
    return actual == _parse_version(requirement.removeprefix("=="))


def _parse_manifest(value: Any, source_path: Path) -> PluginManifest:
    if not isinstance(value, dict):
        raise RegistryError("插件清单必须是对象")
    if value.get("api_version") != "company.platform/v1":
        raise RegistryError(f"插件 {source_path} 的 api_version 不受支持")
    plugin_id = _nonempty_string(value.get("id"), "插件 ID", 160)
    if not SAFE_PLUGIN_ID.fullmatch(plugin_id):
        raise RegistryError(f"插件 ID 无效：{plugin_id}")
    version = _nonempty_string(value.get("version"), f"插件 {plugin_id} 版本", 32)
    _parse_version(version)
    kind = value.get("kind")
    if kind not in {"platform-capability", "business-domain"}:
        raise RegistryError(f"插件 {plugin_id} 的 kind 无效")
    if kind == "platform-capability" and not plugin_id.startswith("platform."):
        raise RegistryError(f"平台能力 {plugin_id} 必须使用 platform 命名空间")
    if kind == "business-domain" and plugin_id.startswith("platform."):
        raise RegistryError(f"业务域 {plugin_id} 不得占用 platform 命名空间")
    raw_dependencies = value.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise RegistryError(f"插件 {plugin_id} dependencies 必须是数组")
    dependencies: list[PluginDependency] = []
    for item in raw_dependencies:
        if not isinstance(item, dict):
            raise RegistryError(f"插件 {plugin_id} 依赖无效")
        dependencies.append(
            PluginDependency(
                _nonempty_string(item.get("id"), f"插件 {plugin_id} 依赖 ID", 160),
                _nonempty_string(item.get("version"), f"插件 {plugin_id} 依赖版本", 32),
            )
        )
    permissions = _string_tuple(value.get("permissions"), f"插件 {plugin_id} 权限")
    write_permissions = _string_tuple(
        value.get("write_permissions"), f"插件 {plugin_id} 写权限"
    )
    excess_write_permissions = sorted(set(write_permissions) - set(permissions))
    if excess_write_permissions:
        raise RegistryError(
            f"插件 {plugin_id} 写权限未在 permissions 声明：{', '.join(excess_write_permissions)}"
        )
    raw_tools = value.get("tools")
    if not isinstance(raw_tools, list):
        raise RegistryError(f"插件 {plugin_id} tools 必须是数组")
    tools: list[PluginTool] = []
    seen_tools: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            raise RegistryError(f"插件 {plugin_id} 工具声明无效")
        if set(item) != {"name", "effect", "permissions"}:
            raise RegistryError(f"插件 {plugin_id} 工具声明字段无效")
        name = _nonempty_string(item.get("name"), f"插件 {plugin_id} 工具名", 160)
        if not SAFE_PLUGIN_ID.fullmatch(name) or name in seen_tools:
            raise RegistryError(f"插件 {plugin_id} 工具名无效或重复：{name}")
        seen_tools.add(name)
        tool_permissions = _string_tuple(
            item.get("permissions"), f"插件 {plugin_id}/{name} 工具权限"
        )
        if not tool_permissions:
            raise RegistryError(f"插件 {plugin_id}/{name} 工具权限不能为空")
        excess = sorted(set(tool_permissions) - set(permissions))
        if excess:
            raise RegistryError(f"插件 {plugin_id}/{name} 工具越权：{', '.join(excess)}")
        effect = item.get("effect")
        if effect not in {"read", "write"}:
            raise RegistryError(f"插件 {plugin_id}/{name} 工具 effect 必须是 read 或 write")
        structured_permissions = set(tool_permissions) & set(write_permissions)
        if effect == "read" and structured_permissions:
            raise RegistryError(
                f"插件 {plugin_id}/{name} 只读工具不得持有写权限："
                f"{', '.join(sorted(structured_permissions))}"
            )
        if effect == "write" and not structured_permissions:
            raise RegistryError(f"插件 {plugin_id}/{name} 写工具必须持有至少一个 write_permissions 权限")
        tools.append(PluginTool(name=name, effect=effect, permissions=tool_permissions))
    configuration = value.get("configuration", {})
    navigation = value.get("navigation", {})
    data_scope = value.get("data_scope", {})
    if not all(isinstance(item, dict) for item in (configuration, navigation, data_scope)):
        raise RegistryError(f"插件 {plugin_id} 的扩展配置必须是对象")
    skills = _string_tuple(value.get("skills"), f"插件 {plugin_id} 技能")
    invalid_skills = [skill for skill in skills if not SAFE_PLUGIN_ID.fullmatch(skill)]
    if invalid_skills:
        raise RegistryError(f"插件 {plugin_id} 技能名无效：{', '.join(invalid_skills)}")
    return PluginManifest(
        api_version=value["api_version"],
        id=plugin_id,
        version=version,
        kind=kind,
        display_name=_nonempty_string(value.get("display_name"), f"插件 {plugin_id} 显示名称", 100),
        description=_nonempty_string(value.get("description"), f"插件 {plugin_id} 描述", 500),
        permissions=permissions,
        write_permissions=write_permissions,
        tools=tuple(tools),
        dependencies=tuple(dependencies),
        capabilities=_string_tuple(value.get("capabilities"), f"插件 {plugin_id} 能力"),
        skills=skills,
        workflows=_string_tuple(value.get("workflows"), f"插件 {plugin_id} 工作流"),
        configuration=dict(configuration),
        navigation=dict(navigation),
        data_scope=dict(data_scope),
        source_path=source_path,
    )


def _parse_workflow(value: Any, manifest: PluginManifest, source_path: Path) -> Workflow:
    if not isinstance(value, dict):
        raise RegistryError("工作流必须是对象")
    workflow_id = _nonempty_string(value.get("id"), "工作流 ID", 180)
    plugin_id = _nonempty_string(value.get("plugin"), f"工作流 {workflow_id} 插件", 160)
    if plugin_id != manifest.id:
        raise RegistryError(f"工作流 {workflow_id} 与插件 {manifest.id} 不匹配")
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise RegistryError(f"工作流 {workflow_id} 没有节点")
    nodes: list[WorkflowNode] = []
    seen: set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise RegistryError(f"工作流 {workflow_id} 节点必须是对象")
        node_id = _nonempty_string(raw_node.get("id"), f"工作流 {workflow_id} 节点 ID", 128)
        if not SAFE_NODE_ID.fullmatch(node_id) or node_id in seen:
            raise RegistryError(f"工作流 {workflow_id} 节点 ID 无效或重复：{node_id}")
        seen.add(node_id)
        node_type = raw_node.get("type")
        if node_type not in NODE_TYPES:
            raise RegistryError(f"工作流 {workflow_id}/{node_id} 节点类型无效")
        dependencies = _string_tuple(raw_node.get("depends_on"), f"工作流 {workflow_id}/{node_id} 依赖")
        permissions = _string_tuple(raw_node.get("permissions"), f"工作流 {workflow_id}/{node_id} 权限")
        excess = sorted(set(permissions) - set(manifest.permissions))
        if excess:
            raise RegistryError(f"工作流 {workflow_id}/{node_id} 越权：{', '.join(excess)}")
        tool = raw_node.get("tool")
        skill = raw_node.get("skill")
        policy = raw_node.get("policy")
        check = raw_node.get("check")
        if node_type == "tool":
            declared_tool = manifest.tool_map.get(tool) if isinstance(tool, str) else None
            if declared_tool is None or set(declared_tool.permissions) != set(permissions):
                raise RegistryError(
                    f"工作流 {workflow_id}/{node_id} 工具未知或节点权限与清单不一致"
                )
        if node_type == "agent" and (not isinstance(skill, str) or skill not in manifest.skills):
            raise RegistryError(f"工作流 {workflow_id}/{node_id} 使用未声明技能")
        if node_type == "approval" and (not isinstance(policy, str) or not policy.strip()):
            raise RegistryError(f"工作流 {workflow_id}/{node_id} 缺少审批策略")
        if node_type == "validator" and (not isinstance(check, str) or not check.strip()):
            raise RegistryError(f"工作流 {workflow_id}/{node_id} 缺少验证规则")
        boundary = raw_node.get("boundary")
        if node_type == "subagent":
            if not isinstance(boundary, dict) or boundary.get("write_scope") != []:
                raise RegistryError(f"工作流 {workflow_id}/{node_id} 子智能体必须具有只读边界")
            turns = boundary.get("max_turns")
            if not isinstance(turns, int) or not 1 <= turns <= 20:
                raise RegistryError(f"工作流 {workflow_id}/{node_id} 子智能体轮数无效")
        nodes.append(
            WorkflowNode(
                id=node_id,
                type=node_type,
                depends_on=dependencies,
                permissions=permissions,
                tool=tool if isinstance(tool, str) else None,
                skill=skill if isinstance(skill, str) else None,
                policy=policy if isinstance(policy, str) else None,
                check=check if isinstance(check, str) else None,
                boundary=boundary if isinstance(boundary, dict) else None,
            )
        )
    workflow = Workflow(
        id=workflow_id,
        plugin=plugin_id,
        display_name=_nonempty_string(value.get("display_name"), f"工作流 {workflow_id} 显示名称", 100),
        description=str(value.get("description", "")).strip(),
        entry_nodes=_string_tuple(value.get("entry_nodes"), f"工作流 {workflow_id} 入口"),
        output_nodes=_string_tuple(value.get("output_nodes"), f"工作流 {workflow_id} 输出"),
        nodes=tuple(nodes),
        source_path=source_path,
    )
    validate_workflow(workflow, manifest)
    return workflow


def plan_workflow(workflow: Workflow) -> list[list[WorkflowNode]]:
    nodes = workflow.node_map
    remaining = {node.id: len(node.depends_on) for node in workflow.nodes}
    successors: dict[str, list[str]] = {node.id: [] for node in workflow.nodes}
    for node in workflow.nodes:
        for dependency in node.depends_on:
            successors.setdefault(dependency, []).append(node.id)
    frontier = sorted(node_id for node_id, count in remaining.items() if count == 0)
    stages: list[list[WorkflowNode]] = []
    visited = 0
    while frontier:
        stages.append([nodes[node_id] for node_id in frontier])
        visited += len(frontier)
        next_frontier: list[str] = []
        for node_id in frontier:
            for successor in successors.get(node_id, []):
                remaining[successor] -= 1
                if remaining[successor] == 0:
                    next_frontier.append(successor)
        frontier = sorted(next_frontier)
    if visited != len(workflow.nodes):
        raise RegistryError(f"工作流 {workflow.id} 包含环或不可达依赖")
    return stages


def validate_workflow(workflow: Workflow, manifest: PluginManifest) -> None:
    nodes = workflow.node_map
    write_permissions = set(manifest.write_permissions)

    def is_write_tool(node: WorkflowNode) -> bool:
        declared = manifest.tool_map.get(node.tool or "")
        return node.type == "tool" and declared is not None and declared.effect == "write"

    for node in workflow.nodes:
        if node.type != "tool" and set(node.permissions) & write_permissions:
            raise RegistryError(
                f"工作流 {workflow.id}/{node.id} 的结构化写权限只能声明在 Tool 节点"
            )
        if node.type == "tool":
            declared = manifest.tool_map.get(node.tool or "")
            if declared is None or set(declared.permissions) != set(node.permissions):
                raise RegistryError(
                    f"工作流 {workflow.id}/{node.id} 工具未知或节点权限与清单不一致"
                )
        if any(dependency not in nodes or dependency == node.id for dependency in node.depends_on):
            raise RegistryError(f"工作流 {workflow.id}/{node.id} 依赖无效")
        if node.type == "join" and len(node.depends_on) < 2:
            raise RegistryError(f"工作流 {workflow.id}/{node.id} join 至少需要两个依赖")
    plan_workflow(workflow)
    actual_entries = sorted(node.id for node in workflow.nodes if not node.depends_on)
    successor_count = {node.id: 0 for node in workflow.nodes}
    for node in workflow.nodes:
        for dependency in node.depends_on:
            successor_count[dependency] += 1
    actual_outputs = sorted(node_id for node_id, count in successor_count.items() if count == 0)
    if sorted(workflow.entry_nodes) != actual_entries:
        raise RegistryError(f"工作流 {workflow.id} entry_nodes 与 DAG 不一致")
    if sorted(workflow.output_nodes) != actual_outputs:
        raise RegistryError(f"工作流 {workflow.id} output_nodes 与 DAG 不一致")
    for node in workflow.nodes:
        if not is_write_tool(node):
            continue
        if len(node.depends_on) != 1 or nodes[node.depends_on[0]].type != "approval":
            raise RegistryError(
                f"工作流 {workflow.id}/{node.id} 的结构化写入必须只有一个直接审批前驱"
            )
        approval = nodes[node.depends_on[0]]
        if len(approval.depends_on) != 1 or nodes[approval.depends_on[0]].type not in {"agent", "validator"}:
            raise RegistryError(
                f"工作流 {workflow.id}/{approval.id} 的审批必须直接跟随分析或验证节点"
            )
        protected_writes = [
            candidate
            for candidate in workflow.nodes
            if is_write_tool(candidate) and approval.id in candidate.depends_on
        ]
        if len(protected_writes) != 1:
            raise RegistryError(f"工作流 {workflow.id}/{approval.id} 必须只保护一个直接写入节点")


def _dependency_order(plugins: Iterable[PluginManifest]) -> None:
    by_id = {plugin.id: plugin for plugin in plugins}
    for plugin in by_id.values():
        for dependency in plugin.dependencies:
            installed = by_id.get(dependency.id)
            if installed is None:
                raise RegistryError(f"插件 {plugin.id} 缺少依赖 {dependency.id}")
            if not satisfies_version(installed.version, dependency.version):
                raise RegistryError(f"插件 {plugin.id} 的依赖 {dependency.id} 版本不兼容")
    remaining = {plugin.id: {item.id for item in plugin.dependencies} for plugin in by_id.values()}
    ready = sorted(plugin_id for plugin_id, dependencies in remaining.items() if not dependencies)
    visited: list[str] = []
    while ready:
        current = ready.pop(0)
        visited.append(current)
        for plugin_id, dependencies in remaining.items():
            if current in dependencies:
                dependencies.remove(current)
                if not dependencies and plugin_id not in visited and plugin_id not in ready:
                    ready.append(plugin_id)
        ready.sort()
    if len(visited) != len(by_id):
        raise RegistryError("插件依赖包含环")


def load_registry(project_root: Path | str | None = None) -> PluginRegistry:
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    plugins_root = (root / "plugins").resolve(strict=True)
    manifest_paths = sorted(
        path for path in plugins_root.rglob("plugin.json") if path.is_file() and not path.is_symlink()
    )
    if not manifest_paths:
        raise RegistryError(f"未在 {plugins_root} 找到插件")
    plugins: dict[str, PluginManifest] = {}
    for path in manifest_paths:
        manifest = _parse_manifest(_read_json(path, plugins_root), path)
        if manifest.id in plugins:
            raise RegistryError(f"插件 ID 重复：{manifest.id}")
        plugins[manifest.id] = manifest
    _dependency_order(plugins.values())
    workflows: dict[str, Workflow] = {}
    for manifest in plugins.values():
        plugin_root = manifest.source_path.parent.resolve(strict=True)
        for relative_path in manifest.workflows:
            candidate = Path(relative_path)
            if candidate.is_absolute():
                raise RegistryError(f"插件 {manifest.id} 工作流路径不能是绝对路径")
            workflow_path = (plugin_root / candidate).resolve(strict=True)
            workflow = _parse_workflow(_read_json(workflow_path, plugin_root), manifest, workflow_path)
            if workflow.id in workflows:
                raise RegistryError(f"工作流 ID 重复：{workflow.id}")
            workflows[workflow.id] = workflow
    return PluginRegistry(root, plugins, workflows)
