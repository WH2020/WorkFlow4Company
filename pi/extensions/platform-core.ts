import { readFileSync, readdirSync, lstatSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export type PluginKind = "platform-capability" | "business-domain";
export type NodeType = "agent" | "tool" | "subagent" | "approval" | "parallel" | "join" | "validator";

export type PluginDependency = {
  id: string;
  version: string;
};

export type PluginTool = {
  name: string;
  permissions: string[];
};

export type PluginManifest = {
  api_version: "company.platform/v1";
  id: string;
  version: string;
  kind: PluginKind;
  display_name: string;
  description: string;
  permissions: string[];
  tools: PluginTool[];
  dependencies: PluginDependency[];
  capabilities: string[];
  skills: string[];
  workflows: string[];
  configuration?: {
    mode?: string;
    requires_user_configuration?: boolean;
  };
  navigation?: {
    group?: string;
    label?: string;
    order?: number;
  };
  data_scope?: {
    tenant?: string;
    domain?: string;
    project?: string;
  };
};

export type WorkflowNode = {
  id: string;
  type: NodeType;
  depends_on: string[];
  permissions: string[];
  tool?: string;
  skill?: string;
  policy?: string;
  check?: string;
  boundary?: {
    objective: string;
    allowed_tools: string[];
    max_turns: number;
    write_scope: string[];
  };
};

export type RuntimeWorkflow = {
  id: string;
  plugin: string;
  display_name: string;
  description?: string;
  entry_nodes: string[];
  output_nodes: string[];
  nodes: WorkflowNode[];
};

export type PluginBundle = {
  root: string;
  plugins: Map<string, PluginManifest>;
  workflows: Map<string, RuntimeWorkflow>;
};

export type CompanyProfile = {
  id: string;
  display_name: string;
  description: string;
  enabled_domains: string[];
  available_domains: string[];
  default_view: string;
  default_workflow: string | null;
  roles: string[];
};

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SAFE_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/u;
const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u;
const NODE_TYPES = new Set<NodeType>([
  "agent",
  "tool",
  "subagent",
  "approval",
  "parallel",
  "join",
  "validator",
]);

function assertString(value: unknown, label: string, maxLength = 500): asserts value is string {
  if (typeof value !== "string" || !value.trim() || value.length > maxLength) {
    throw new Error(`${label} 必须是 1-${maxLength} 字的非空字符串`);
  }
}

function assertStringArray(value: unknown, label: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error(`${label} 必须是非空字符串数组`);
  }
  if (new Set(value).size !== value.length) throw new Error(`${label} 不能包含重复值`);
}

function contained(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel));
}

function readJson(path: string, containmentRoot: string): unknown {
  const resolvedRoot = realpathSync.native(containmentRoot);
  const resolvedPath = realpathSync.native(path);
  if (!contained(resolvedRoot, resolvedPath)) throw new Error(`插件路径越界：${path}`);
  const metadata = lstatSync(resolvedPath);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 1024 * 1024) {
    throw new Error(`插件 JSON 不是受限普通文件：${path}`);
  }
  return JSON.parse(readFileSync(resolvedPath, "utf8")) as unknown;
}

function findManifestPaths(root: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) found.push(...findManifestPaths(path));
    if (entry.isFile() && entry.name === "plugin.json") found.push(path);
  }
  return found.sort();
}

function parseVersion(value: string): [number, number, number] {
  const match = SEMVER.exec(value);
  if (!match) throw new Error(`不支持的语义版本：${value}`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function compareVersion(left: [number, number, number], right: [number, number, number]): number {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index]! - right[index]!;
  }
  return 0;
}

export function satisfiesVersion(version: string, requirement: string): boolean {
  const actual = parseVersion(version);
  const operator = requirement.startsWith(">=") ? ">=" : requirement.startsWith("^") ? "^" : "=";
  const desired = parseVersion(requirement.replace(/^(>=|\^|==)/u, ""));
  if (operator === ">=") return compareVersion(actual, desired) >= 0;
  if (operator === "^") {
    const upper: [number, number, number] = desired[0] > 0
      ? [desired[0] + 1, 0, 0]
      : desired[1] > 0
        ? [0, desired[1] + 1, 0]
        : [0, 0, desired[2] + 1];
    return compareVersion(actual, desired) >= 0 && compareVersion(actual, upper) < 0;
  }
  return compareVersion(actual, desired) === 0;
}

export function assertPluginManifest(value: unknown): PluginManifest {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("插件清单必须是对象");
  const manifest = value as Partial<PluginManifest>;
  if (manifest.api_version !== "company.platform/v1") throw new Error("插件 api_version 不受支持");
  assertString(manifest.id, "插件 ID", 160);
  if (!SAFE_ID.test(manifest.id)) throw new Error(`插件 ID 无效：${manifest.id}`);
  assertString(manifest.version, "插件版本", 32);
  parseVersion(manifest.version);
  if (manifest.kind !== "platform-capability" && manifest.kind !== "business-domain") {
    throw new Error(`插件 ${manifest.id} 的 kind 无效`);
  }
  assertString(manifest.display_name, `插件 ${manifest.id} 显示名称`, 100);
  assertString(manifest.description, `插件 ${manifest.id} 描述`, 500);
  assertStringArray(manifest.permissions, `插件 ${manifest.id} 权限`);
  if (!Array.isArray(manifest.tools)) throw new Error(`插件 ${manifest.id} tools 必须是数组`);
  const toolNames = new Set<string>();
  for (const tool of manifest.tools) {
    if (!tool || typeof tool !== "object") throw new Error(`插件 ${manifest.id} 工具声明无效`);
    assertString(tool.name, `插件 ${manifest.id} 工具名`, 160);
    if (!SAFE_ID.test(tool.name) || toolNames.has(tool.name)) {
      throw new Error(`插件 ${manifest.id} 工具名无效或重复：${tool.name}`);
    }
    toolNames.add(tool.name);
    assertStringArray(tool.permissions, `插件 ${manifest.id}/${tool.name} 工具权限`);
    if (tool.permissions.length === 0) throw new Error(`插件 ${manifest.id}/${tool.name} 工具权限不能为空`);
    const excess = tool.permissions.filter((permission) => !manifest.permissions!.includes(permission));
    if (excess.length > 0) throw new Error(`插件 ${manifest.id}/${tool.name} 工具越权：${excess.join(", ")}`);
  }
  assertStringArray(manifest.capabilities, `插件 ${manifest.id} 能力`);
  assertStringArray(manifest.skills, `插件 ${manifest.id} 技能`);
  assertStringArray(manifest.workflows, `插件 ${manifest.id} 工作流`);
  if (!Array.isArray(manifest.dependencies)) throw new Error(`插件 ${manifest.id} dependencies 必须是数组`);
  for (const dependency of manifest.dependencies) {
    if (!dependency || typeof dependency !== "object") throw new Error(`插件 ${manifest.id} 依赖无效`);
    assertString(dependency.id, `插件 ${manifest.id} 依赖 ID`, 160);
    assertString(dependency.version, `插件 ${manifest.id} 依赖版本`, 32);
  }
  if (manifest.kind === "business-domain" && manifest.id.startsWith("platform.")) {
    throw new Error(`业务域 ${manifest.id} 不得占用平台命名空间`);
  }
  if (manifest.kind === "platform-capability" && !manifest.id.startsWith("platform.")) {
    throw new Error(`平台能力 ${manifest.id} 必须使用 platform 命名空间`);
  }
  return manifest as PluginManifest;
}

export function planWorkflow(workflow: RuntimeWorkflow): WorkflowNode[][] {
  const nodes = new Map(workflow.nodes.map((node) => [node.id, node]));
  const remaining = new Map(workflow.nodes.map((node) => [node.id, node.depends_on.length]));
  const successors = new Map(workflow.nodes.map((node) => [node.id, [] as string[]]));
  for (const node of workflow.nodes) {
    for (const dependency of node.depends_on) successors.get(dependency)?.push(node.id);
  }
  let frontier = workflow.nodes.filter((node) => remaining.get(node.id) === 0).map((node) => node.id).sort();
  const stages: WorkflowNode[][] = [];
  let visited = 0;
  while (frontier.length > 0) {
    stages.push(frontier.map((id) => nodes.get(id)!));
    visited += frontier.length;
    const next: string[] = [];
    for (const id of frontier) {
      for (const successor of successors.get(id) ?? []) {
        const count = remaining.get(successor)! - 1;
        remaining.set(successor, count);
        if (count === 0) next.push(successor);
      }
    }
    frontier = next.sort();
  }
  if (visited !== workflow.nodes.length) throw new Error(`工作流 ${workflow.id} 包含环或不可达依赖`);
  return stages;
}

export function validateRuntimeWorkflow(workflow: RuntimeWorkflow, manifest: PluginManifest): void {
  assertString(workflow.id, "工作流 ID", 180);
  assertString(workflow.plugin, `工作流 ${workflow.id} 插件`, 160);
  assertString(workflow.display_name, `工作流 ${workflow.id} 显示名称`, 100);
  if (workflow.plugin !== manifest.id) throw new Error(`工作流 ${workflow.id} 与插件 ${manifest.id} 不匹配`);
  assertStringArray(workflow.entry_nodes, `工作流 ${workflow.id} 入口`);
  assertStringArray(workflow.output_nodes, `工作流 ${workflow.id} 输出`);
  if (!Array.isArray(workflow.nodes) || workflow.nodes.length === 0) throw new Error(`工作流 ${workflow.id} 没有节点`);

  const declaredPermissions = new Set(manifest.permissions);
  const declaredSkills = new Set(manifest.skills);
  const declaredTools = new Map(manifest.tools.map((tool) => [tool.name, tool]));
  const nodes = new Map<string, WorkflowNode>();
  for (const node of workflow.nodes) {
    assertString(node.id, `工作流 ${workflow.id} 节点 ID`, 128);
    if (nodes.has(node.id)) throw new Error(`工作流 ${workflow.id} 节点 ID 重复：${node.id}`);
    if (!NODE_TYPES.has(node.type)) throw new Error(`工作流 ${workflow.id}/${node.id} 节点类型无效`);
    assertStringArray(node.depends_on, `工作流 ${workflow.id}/${node.id} 依赖`);
    assertStringArray(node.permissions, `工作流 ${workflow.id}/${node.id} 权限`);
    const excess = node.permissions.filter((permission) => !declaredPermissions.has(permission));
    if (excess.length > 0) throw new Error(`工作流 ${workflow.id}/${node.id} 越权：${excess.join(", ")}`);
    const structuredPermissions = node.permissions.filter((permission) => permission.endsWith(".write"));
    if (structuredPermissions.length > 0 && node.type !== "tool") {
      throw new Error(`工作流 ${workflow.id}/${node.id} 的结构化写权限只能声明在 Tool 节点`);
    }
    if (node.type === "agent" && (!node.skill || !declaredSkills.has(node.skill))) {
      throw new Error(`工作流 ${workflow.id}/${node.id} 使用未声明技能`);
    }
    if (node.type === "tool") {
      const declaredTool = node.tool ? declaredTools.get(node.tool) : undefined;
      const required = declaredTool ? [...declaredTool.permissions].sort() : undefined;
      const actual = [...node.permissions].sort();
      if (!required || JSON.stringify(required) !== JSON.stringify(actual)) {
        throw new Error(`工作流 ${workflow.id}/${node.id} 工具未知或节点权限与清单不一致`);
      }
    }
    if (node.type === "approval" && !node.policy) throw new Error(`工作流 ${workflow.id}/${node.id} 缺少审批策略`);
    if (node.type === "validator" && !node.check) throw new Error(`工作流 ${workflow.id}/${node.id} 缺少验证规则`);
    if (node.type === "join" && node.depends_on.length < 2) throw new Error(`工作流 ${workflow.id}/${node.id} join 至少需要两个依赖`);
    if (node.type === "subagent") {
      const boundary = node.boundary;
      if (!boundary || !boundary.objective.trim() || boundary.write_scope.length !== 0) {
        throw new Error(`工作流 ${workflow.id}/${node.id} 子智能体必须具有只读边界`);
      }
      if (!Number.isInteger(boundary.max_turns) || boundary.max_turns < 1 || boundary.max_turns > 20) {
        throw new Error(`工作流 ${workflow.id}/${node.id} 子智能体轮数无效`);
      }
    }
    nodes.set(node.id, node);
  }

  for (const node of nodes.values()) {
    if (node.depends_on.some((dependency) => !nodes.has(dependency) || dependency === node.id)) {
      throw new Error(`工作流 ${workflow.id}/${node.id} 依赖无效`);
    }
  }
  planWorkflow(workflow);

  const actualEntries = [...nodes.values()].filter((node) => node.depends_on.length === 0).map((node) => node.id).sort();
  const successorCount = new Map([...nodes.keys()].map((id) => [id, 0]));
  for (const node of nodes.values()) {
    for (const dependency of node.depends_on) successorCount.set(dependency, successorCount.get(dependency)! + 1);
  }
  const actualOutputs = [...successorCount].filter(([, count]) => count === 0).map(([id]) => id).sort();
  if (JSON.stringify([...workflow.entry_nodes].sort()) !== JSON.stringify(actualEntries)) {
    throw new Error(`工作流 ${workflow.id} entry_nodes 与 DAG 不一致`);
  }
  if (JSON.stringify([...workflow.output_nodes].sort()) !== JSON.stringify(actualOutputs)) {
    throw new Error(`工作流 ${workflow.id} output_nodes 与 DAG 不一致`);
  }

  for (const node of nodes.values()) {
    const structuredWrite = node.type === "tool" && node.permissions.some((permission) => permission.endsWith(".write"));
    if (!structuredWrite) continue;
    if (node.depends_on.length !== 1 || nodes.get(node.depends_on[0]!)?.type !== "approval") {
      throw new Error(`工作流 ${workflow.id}/${node.id} 的结构化写入必须只有一个直接审批前驱`);
    }
    const approval = nodes.get(node.depends_on[0]!)!;
    if (approval.depends_on.length !== 1 || !["agent", "validator"].includes(nodes.get(approval.depends_on[0]!)?.type ?? "")) {
      throw new Error(`工作流 ${workflow.id}/${approval.id} 的审批必须直接跟随分析或验证节点`);
    }
    const protectedWrites = [...nodes.values()].filter(
      (candidate) => candidate.type === "tool" && candidate.depends_on.includes(approval.id) && candidate.permissions.some((permission) => permission.endsWith(".write")),
    );
    if (protectedWrites.length !== 1) throw new Error(`工作流 ${workflow.id}/${approval.id} 必须只保护一个直接写入节点`);
  }
}

export function assertPluginDependencies(plugins: Map<string, PluginManifest>): void {
  for (const manifest of plugins.values()) {
    for (const dependency of manifest.dependencies) {
      const installed = plugins.get(dependency.id);
      if (!installed) throw new Error(`插件 ${manifest.id} 缺少依赖 ${dependency.id}`);
      if (!satisfiesVersion(installed.version, dependency.version)) {
        throw new Error(`插件 ${manifest.id} 的依赖 ${dependency.id} 版本不兼容`);
      }
    }
  }
  const remaining = new Map(
    [...plugins.values()].map((plugin) => [plugin.id, new Set(plugin.dependencies.map((item) => item.id))]),
  );
  const ready = [...remaining.entries()]
    .filter(([, dependencies]) => dependencies.size === 0)
    .map(([id]) => id)
    .sort();
  const visited = new Set<string>();
  while (ready.length > 0) {
    const current = ready.shift()!;
    visited.add(current);
    for (const [pluginId, dependencies] of remaining) {
      if (!dependencies.delete(current)) continue;
      if (dependencies.size === 0 && !visited.has(pluginId) && !ready.includes(pluginId)) {
        ready.push(pluginId);
        ready.sort();
      }
    }
  }
  if (visited.size !== plugins.size) throw new Error("插件依赖包含环");
}

export function loadPluginBundle(root = packageRoot): PluginBundle {
  const pluginsRoot = join(root, "plugins");
  const plugins = new Map<string, PluginManifest>();
  const manifestPaths = findManifestPaths(pluginsRoot);
  for (const manifestPath of manifestPaths) {
    const manifest = assertPluginManifest(readJson(manifestPath, pluginsRoot));
    if (plugins.has(manifest.id)) throw new Error(`插件 ID 重复：${manifest.id}`);
    plugins.set(manifest.id, manifest);
  }
  if (plugins.size === 0) throw new Error(`未在 ${pluginsRoot} 找到插件`);

  const workflows = new Map<string, RuntimeWorkflow>();
  for (const manifestPath of manifestPaths) {
    const manifest = plugins.get((readJson(manifestPath, pluginsRoot) as PluginManifest).id)!;
    const pluginRoot = dirname(manifestPath);
    for (const workflowPath of manifest.workflows) {
      if (isAbsolute(workflowPath)) throw new Error(`插件 ${manifest.id} 工作流路径不能是绝对路径`);
      const resolved = resolve(pluginRoot, workflowPath);
      const workflow = readJson(resolved, pluginRoot) as RuntimeWorkflow;
      validateRuntimeWorkflow(workflow, manifest);
      if (workflows.has(workflow.id)) throw new Error(`工作流 ID 重复：${workflow.id}`);
      workflows.set(workflow.id, workflow);
    }
  }

  assertPluginDependencies(plugins);
  return { root, plugins, workflows };
}

export function loadCompanyProfile(
  root: string,
  bundle: PluginBundle,
  profileId = "company-manager",
): CompanyProfile {
  if (!SAFE_ID.test(profileId)) throw new Error(`Profile ID 无效：${profileId}`);
  const profilesRoot = join(root, "profiles");
  const value = readJson(join(profilesRoot, profileId, "profile.json"), profilesRoot);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Profile 必须是对象");
  const profile = value as Partial<CompanyProfile>;
  const keys = new Set(Object.keys(profile));
  const expected = [
    "id",
    "display_name",
    "description",
    "enabled_domains",
    "available_domains",
    "default_view",
    "default_workflow",
    "roles",
  ];
  if (expected.some((key) => !keys.has(key)) || [...keys].some((key) => !expected.includes(key))) {
    throw new Error(`Profile ${profileId} 字段无效`);
  }
  assertString(profile.id, "Profile ID", 80);
  if (profile.id !== profileId) throw new Error(`Profile 目录与 ID 不一致：${profileId}/${profile.id}`);
  assertString(profile.display_name, "Profile 显示名称", 100);
  assertString(profile.description, "Profile 描述", 500);
  assertStringArray(profile.enabled_domains, `Profile ${profileId} 已启用业务域`);
  assertStringArray(profile.available_domains, `Profile ${profileId} 可用业务域`);
  assertString(profile.default_view, "Profile 默认视图", 100);
  assertStringArray(profile.roles, `Profile ${profileId} 角色`);
  if (!profile.enabled_domains.every((domainId) => profile.available_domains!.includes(domainId))) {
    throw new Error(`Profile ${profileId} 启用的业务域必须先声明为可用`);
  }
  const installedDomains = new Set(
    [...bundle.plugins.values()].filter((plugin) => plugin.kind === "business-domain").map((plugin) => plugin.id),
  );
  const unavailableEnabled = profile.enabled_domains.filter((domainId) => !installedDomains.has(domainId));
  if (unavailableEnabled.length > 0) {
    throw new Error(`Profile ${profileId} 启用了未安装业务域：${unavailableEnabled.join(", ")}`);
  }
  if (profile.default_workflow !== null) {
    assertString(profile.default_workflow, "默认工作流", 180);
    const workflow = bundle.workflows.get(profile.default_workflow);
    if (!workflow) throw new Error(`Profile ${profileId} 默认工作流未安装：${profile.default_workflow}`);
    if (!profile.enabled_domains.includes(workflow.plugin)) {
      throw new Error(`Profile ${profileId} 默认工作流所属业务域未启用`);
    }
  }
  return profile as CompanyProfile;
}

export function bundleSummary(bundle: PluginBundle, profile?: CompanyProfile): object {
  const plugins = [...bundle.plugins.values()].map((plugin) => ({
    id: plugin.id,
    display_name: plugin.display_name,
    description: plugin.description,
    kind: plugin.kind,
    version: plugin.version,
    capabilities: plugin.capabilities,
    requires_user_configuration: plugin.configuration?.requires_user_configuration ?? false,
    configuration_mode: plugin.configuration?.mode ?? "built-in",
  }));
  const availableDomains = new Set(
    profile?.available_domains ?? plugins.filter((plugin) => plugin.kind === "business-domain").map((plugin) => plugin.id),
  );
  const enabledDomains = new Set(profile?.enabled_domains ?? availableDomains);
  return {
    profile,
    platform_capabilities: plugins.filter((plugin) => plugin.kind === "platform-capability"),
    business_domains: plugins
      .filter((plugin) => plugin.kind === "business-domain" && availableDomains.has(plugin.id))
      .map((plugin) => ({ ...plugin, enabled: enabledDomains.has(plugin.id) })),
    workflows: [...bundle.workflows.values()]
      .filter((workflow) => enabledDomains.has(workflow.plugin))
      .map((workflow) => ({
        id: workflow.id,
        plugin: workflow.plugin,
        display_name: workflow.display_name,
        stages: planWorkflow(workflow).map((stage) => stage.map((node) => node.id)),
      })),
  };
}
