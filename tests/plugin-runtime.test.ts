import assert from "node:assert/strict";
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import test from "node:test";
import { DefaultPackageManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import {
  assertPluginDependencies,
  assertPluginManifest,
  bundleSummary,
  loadCompanyProfile,
  loadPluginBundle,
  planWorkflow,
  validateRuntimeWorkflow,
  type PluginManifest,
  type RuntimeWorkflow,
} from "../pi/extensions/platform-core.ts";

const root = resolve(import.meta.dirname, "..");

type RpcRecord = {
  id?: string;
  type?: string;
  command?: string;
  success?: boolean;
  error?: string;
  method?: string;
  message?: string;
  data?: { commands?: Array<{ name: string; source: string }> };
};

async function inspectPiResources(
  projectRoot: string,
  profileId: string,
  skillDirectories: string[],
  agentDir: string,
): Promise<{ commands: Array<{ name: string; source: string }>; activeTools: string[] }> {
  const cli = join(root, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js");
  const governedTools = [
    "company_capability_catalog",
    "company_plan_workflow",
    "company_check_domain_permissions",
    "company_library_search",
  ];
  const child = spawn(
    process.execPath,
    [
      cli,
      "--mode",
      "rpc",
      "--approve",
      "--no-extensions",
      "--no-skills",
      "--no-prompt-templates",
      "--no-themes",
      "--no-context-files",
      "--no-session",
      "--offline",
      "--tools",
      governedTools.join(","),
      "--extension",
      join(root, "pi", "extensions", "company-workflow.ts"),
      ...skillDirectories.flatMap((directory) => ["--skill", directory]),
    ],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        AGENT4COMPANY_PROFILE: profileId,
        PI_CODING_AGENT_DIR: agentDir,
        PI_OFFLINE: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  let stderr = "";
  let buffered = "";
  const records: RpcRecord[] = [];
  const waiters: Array<{
    predicate: (record: RpcRecord) => boolean;
    accept: (record: RpcRecord) => void;
    reject: (error: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  }> = [];
  const publish = (record: RpcRecord) => {
    records.push(record);
    for (const waiter of [...waiters]) {
      if (!waiter.predicate(record)) continue;
      clearTimeout(waiter.timer);
      waiters.splice(waiters.indexOf(waiter), 1);
      waiter.accept(record);
    }
  };
  const waitFor = (predicate: (record: RpcRecord) => boolean) => {
    const existing = records.find(predicate);
    if (existing) return Promise.resolve(existing);
    return new Promise<RpcRecord>((accept, reject) => {
      const waiter = {
        predicate,
        accept,
        reject,
        timer: setTimeout(() => {
          waiters.splice(waiters.indexOf(waiter), 1);
          reject(new Error(`Pi RPC 资源响应超时：${stderr}`));
        }, 20_000),
      };
      waiters.push(waiter);
    });
  };
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => { stderr += chunk; });
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    buffered += chunk;
    const lines = buffered.split("\n");
    buffered = lines.pop() || "";
    for (const line of lines) {
      if (line.trim()) publish(JSON.parse(line) as RpcRecord);
    }
  });
  child.once("error", (error) => {
    for (const waiter of waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
  });

  child.stdin.write(`${JSON.stringify({ id: "resource-check", type: "get_commands" })}\n`);
  const commandsResponse = await waitFor((record) => record.id === "resource-check");
  assert.equal(commandsResponse.success, true, commandsResponse.error);
  child.stdin.write(`${JSON.stringify({ id: "status-check", type: "prompt", message: "/company-status" })}\n`);
  const statusResponse = await waitFor((record) => record.id === "status-check");
  assert.equal(statusResponse.success, true, statusResponse.error);
  const statusNotice = await waitFor(
    (record) => record.type === "extension_ui_request"
      && record.method === "notify"
      && record.message?.startsWith("AGENT4COMPANY_STATUS ") === true,
  );
  child.stdin.end();
  const exitCode = await new Promise<number | null>((accept) => child.once("exit", accept));
  assert.equal(exitCode, 0, stderr);
  const status = JSON.parse(statusNotice.message!.replace("AGENT4COMPANY_STATUS ", "")) as {
    profile_id: string;
    active_tools: string[];
  };
  assert.equal(status.profile_id, profileId);
  return {
    commands: commandsResponse.data?.commands ?? [],
    activeTools: status.active_tools,
  };
}

function resolveProfileSkillsWithPython(projectRoot: string, profileId: string): string[] {
  const python = process.platform === "win32"
    ? join(root, ".venv", "Scripts", "python.exe")
    : join(root, ".venv", "bin", "python");
  const program = [
    "import json, sys",
    "from company_platform.plugin_registry import load_registry",
    "from company_platform.profiles import load_profile, resolve_profile_skill_directories",
    "root, profile_id = sys.argv[1], sys.argv[2]",
    "registry = load_registry(root)",
    "profile = load_profile(root, registry, profile_id)",
    "print(json.dumps([str(path) for path in resolve_profile_skill_directories(root, registry, profile)]))",
  ].join("; ");
  const result = spawnSync(python, ["-c", program, projectRoot, profileId], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout) as string[];
}

test("公司级插件目录可加载，销售仅是业务域", () => {
  const bundle = loadPluginBundle(root);
  const platform = [...bundle.plugins.values()].filter((plugin) => plugin.kind === "platform-capability");
  const domains = [...bundle.plugins.values()].filter((plugin) => plugin.kind === "business-domain");
  assert.equal(platform.length, 8);
  assert.equal(bundle.plugins.get("platform.library")?.configuration?.mode, "built-in");
  assert.deepEqual(domains.map((domain) => domain.id), ["domain.sales"]);
  assert.ok(platform.every((plugin) => !plugin.id.includes("sales")));
  assert.equal(bundle.workflows.size, 1);
});

test("销售域首个 DAG 包含直接审批边界并可拓扑规划", () => {
  const bundle = loadPluginBundle(root);
  const workflow = bundle.workflows.get("domain.sales.pipeline-review");
  assert.ok(workflow);
  assert.deepEqual(planWorkflow(workflow).map((stage) => stage.map((node) => node.id)), [
    ["load_sales_context"],
    ["draft_review"],
    ["owner_approval"],
    ["record_actions"],
    ["verify_audit"],
  ]);
  const write = workflow.nodes.find((node) => node.id === "record_actions");
  assert.deepEqual(write?.depends_on, ["owner_approval"]);
});

test("绕过审批的结构化写入被拒绝", () => {
  const bundle = loadPluginBundle(root);
  const manifest = bundle.plugins.get("domain.sales")!;
  const current = bundle.workflows.get("domain.sales.pipeline-review")!;
  const unsafe = structuredClone(current) as RuntimeWorkflow;
  unsafe.nodes.find((node) => node.id === "record_actions")!.depends_on = ["draft_review"];
  unsafe.nodes = unsafe.nodes.filter((node) => node.id !== "owner_approval");
  assert.throws(() => validateRuntimeWorkflow(unsafe, manifest), /结构化写入必须只有一个直接审批前驱/u);
});

test("Agent 节点不能声明结构化写权限", () => {
  const bundle = loadPluginBundle(root);
  const manifest = bundle.plugins.get("domain.sales")!;
  const unsafe = structuredClone(bundle.workflows.get("domain.sales.pipeline-review")!) as RuntimeWorkflow;
  unsafe.nodes.find((node) => node.id === "draft_review")!.permissions.push("sales.write");
  assert.throws(() => validateRuntimeWorkflow(unsafe, manifest), /结构化写权限只能声明在 Tool 节点/u);
});

test("公司 Profile 默认不启用销售，验证组合才贡献销售工作流", () => {
  const bundle = loadPluginBundle(root);
  const profile = loadCompanyProfile(root, bundle, "company-manager");
  assert.equal(profile.id, "company-manager");
  assert.deepEqual(profile.enabled_domains, []);
  assert.deepEqual(profile.available_domains, ["domain.sales"]);
  const defaultSummary = bundleSummary(bundle, profile) as {
    business_domains: Array<{ id: string; enabled: boolean }>;
    workflows: Array<{ id: string }>;
  };
  assert.deepEqual(defaultSummary.business_domains.map((domain) => domain.enabled), [false]);
  assert.deepEqual(defaultSummary.workflows, []);
  const salesProfile = loadCompanyProfile(root, bundle, "company-with-sales");
  const salesSummary = bundleSummary(bundle, salesProfile) as { workflows: Array<{ id: string }> };
  assert.deepEqual(salesSummary.workflows.map((workflow) => workflow.id), ["domain.sales.pipeline-review"]);
});

test("TypeScript 注册表拒绝插件依赖环", () => {
  const manifests = [...loadPluginBundle(root).plugins.values()].slice(0, 2).map(
    (manifest) => structuredClone(manifest) as PluginManifest,
  );
  manifests[0]!.dependencies = [{ id: manifests[1]!.id, version: `^${manifests[1]!.version}` }];
  manifests[1]!.dependencies = [{ id: manifests[0]!.id, version: `^${manifests[0]!.version}` }];
  assert.throws(
    () => assertPluginDependencies(new Map(manifests.map((manifest) => [manifest.id, manifest]))),
    /插件依赖包含环/u,
  );
});

test("新业务域可仅通过清单声明自己的工具", () => {
  const manifest = assertPluginManifest({
    api_version: "company.platform/v1",
    id: "domain.delivery",
    version: "1.0.0",
    kind: "business-domain",
    display_name: "交付管理",
    description: "独立测试业务域。",
    permissions: ["delivery.read"],
    write_permissions: [],
    tools: [{ name: "delivery.read", effect: "read", permissions: ["delivery.read"] }],
    dependencies: [],
    capabilities: ["delivery.review"],
    skills: [],
    workflows: [],
  });
  const workflow: RuntimeWorkflow = {
    id: "domain.delivery.review",
    plugin: manifest.id,
    display_name: "交付复盘",
    description: "读取交付事实。",
    entry_nodes: ["load_context"],
    output_nodes: ["load_context"],
    nodes: [
      {
        id: "load_context",
        type: "tool",
        tool: "delivery.read",
        depends_on: [],
        permissions: ["delivery.read"],
      },
    ],
  };
  assert.doesNotThrow(() => validateRuntimeWorkflow(workflow, manifest));
});

test("工具显式 write effect 对 create/delete/mutate 同样强制审批", () => {
  for (const permission of ["delivery.create", "delivery.delete", "delivery.mutate"]) {
    const manifest = assertPluginManifest({
      api_version: "company.platform/v1",
      id: "domain.delivery",
      version: "1.0.0",
      kind: "business-domain",
      display_name: "交付管理",
      description: "写入效果负向验证。",
      permissions: [permission],
      write_permissions: [permission],
      tools: [{ name: permission, effect: "write", permissions: [permission] }],
      dependencies: [],
      capabilities: ["delivery.change"],
      skills: [],
      workflows: [],
    });
    const workflow: RuntimeWorkflow = {
      id: "domain.delivery.change",
      plugin: manifest.id,
      display_name: "交付变更",
      description: "缺少审批的写入。",
      entry_nodes: ["change"],
      output_nodes: ["change"],
      nodes: [{
        id: "change",
        type: "tool",
        tool: permission,
        depends_on: [],
        permissions: [permission],
      }],
    };
    assert.throws(
      () => validateRuntimeWorkflow(workflow, manifest),
      /结构化写入必须只有一个直接审批前驱/u,
    );
  }
});

test("写工具的只读组合权限仍可由 Agent 单独声明", () => {
  const bundle = loadPluginBundle(root);
  const current = bundle.plugins.get("platform.presentation")!;
  const manifest = structuredClone(current) as PluginManifest;
  manifest.skills = ["read-presentation-context"];
  const workflow: RuntimeWorkflow = {
    id: "platform.presentation.read-context",
    plugin: manifest.id,
    display_name: "读取演示上下文",
    description: "只使用写工具组合权限中的只读部分。",
    entry_nodes: ["read_context"],
    output_nodes: ["read_context"],
    nodes: [{
      id: "read_context",
      type: "agent",
      depends_on: [],
      permissions: ["knowledge.read"],
      skill: "read-presentation-context",
    }],
  };
  assert.deepEqual(manifest.write_permissions, ["presentation.plan.write", "artifact.write"]);
  assert.doesNotThrow(() => validateRuntimeWorkflow(workflow, manifest));
});

test("Pi 清单与隔离 RPC 按 Profile 加载资源且只启用受控工具", { timeout: 45_000 }, async () => {
  const agentDir = mkdtempSync(join(tmpdir(), "agent4company-pi-resources-"));
  try {
    const settings = SettingsManager.create(root, agentDir);
    const manager = new DefaultPackageManager({ cwd: root, agentDir, settingsManager: settings });
    const resolvedResources = await manager.resolveExtensionSources([root], { temporary: true });
    assert.ok(
      resolvedResources.extensions.some((item) => item.enabled && item.path.endsWith("company-workflow.ts")),
    );
    const manifestSkills = resolvedResources.skills
      .filter((item) => item.enabled)
      .map((item) => item.path.replaceAll("\\", "/"));
    assert.ok(manifestSkills.some((path) => path.endsWith("pi/skills/manage-company/SKILL.md")));
    assert.ok(manifestSkills.some((path) => path.endsWith("pi/skills/manage-sales-domain/SKILL.md")));

    const companySkill = join(root, "pi", "skills", "manage-company");
    const salesSkill = join(root, "pi", "skills", "manage-sales-domain");
    const company = await inspectPiResources(
      root,
      "company-manager",
      [companySkill],
      join(agentDir, "company"),
    );
    const sales = await inspectPiResources(
      root,
      "company-with-sales",
      [companySkill, salesSkill],
      join(agentDir, "sales"),
    );
    const extensionNames = (value: typeof company) => value.commands
      .filter((command) => command.source === "extension")
      .map((command) => command.name);
    const skillNames = (value: typeof company) => value.commands
      .filter((command) => command.source === "skill")
      .map((command) => command.name)
      .sort();
    // Pi 0.84.x 始终注册自身隐藏的 llama.cpp 运维命令；它不是用户目录发现的资源，
    // 也不会进入 Agent 的 active_tools。除该内置命令外，只允许公司扩展命令出现。
    assert.deepEqual(extensionNames(company).sort(), ["company-status", "llama"]);
    assert.deepEqual(extensionNames(sales).sort(), ["company-status", "llama"]);
    assert.deepEqual(skillNames(company), ["skill:manage-company"]);
    assert.deepEqual(skillNames(sales), ["skill:manage-company", "skill:manage-sales-domain"]);
    const governedTools = [
      "company_capability_catalog",
      "company_check_domain_permissions",
      "company_library_search",
      "company_plan_workflow",
    ];
    assert.deepEqual([...company.activeTools].sort(), governedTools);
    assert.deepEqual([...sales.activeTools].sort(), governedTools);
    assert.ok(!company.activeTools.some((tool) => ["bash", "edit", "write"].includes(tool)));
  } finally {
    rmSync(agentDir, { recursive: true, force: true });
  }
});

test("交付域 Profile 通过清单贡献真实 Pi Skill，无需销售特例", { timeout: 45_000 }, async () => {
  const temporary = mkdtempSync(join(tmpdir(), "agent4company-delivery-profile-"));
  try {
    const project = join(temporary, "project");
    cpSync(join(root, "plugins", "platform"), join(project, "plugins", "platform"), { recursive: true });
    const domain = join(project, "plugins", "domains", "delivery");
    mkdirSync(domain, { recursive: true });
    writeFileSync(join(domain, "plugin.json"), JSON.stringify({
      api_version: "company.platform/v1",
      id: "domain.delivery",
      version: "1.0.0",
      kind: "business-domain",
      display_name: "交付管理",
      description: "合成交付域。",
      permissions: ["delivery.read"],
      write_permissions: [],
      tools: [{ name: "delivery.read", effect: "read", permissions: ["delivery.read"] }],
      dependencies: [],
      capabilities: ["delivery.review"],
      skills: ["manage-delivery-domain"],
      workflows: [],
    }), "utf8");
    const profileDirectory = join(project, "profiles", "company-with-delivery");
    mkdirSync(profileDirectory, { recursive: true });
    writeFileSync(join(profileDirectory, "profile.json"), JSON.stringify({
      id: "company-with-delivery",
      display_name: "公司管理平台 · 交付域验证",
      description: "合成验证 Profile。",
      enabled_domains: ["domain.delivery"],
      available_domains: ["domain.delivery"],
      default_view: "company-overview",
      default_workflow: null,
      roles: ["company-admin"],
    }), "utf8");
    cpSync(
      join(root, "pi", "skills", "manage-company"),
      join(project, "pi", "skills", "manage-company"),
      { recursive: true },
    );
    const deliverySkill = join(project, "pi", "skills", "manage-delivery-domain");
    mkdirSync(deliverySkill, { recursive: true });
    writeFileSync(
      join(deliverySkill, "SKILL.md"),
      "---\nname: manage-delivery-domain\ndescription: 合成交付域。\n---\n\n# 交付域\n",
      "utf8",
    );
    const skills = resolveProfileSkillsWithPython(project, "company-with-delivery");
    assert.deepEqual(skills.map((path) => path.replaceAll("\\", "/").split("/").at(-1)), [
      "manage-company",
      "manage-delivery-domain",
    ]);
    const inspected = await inspectPiResources(
      project,
      "company-with-delivery",
      skills,
      join(temporary, "agent"),
    );
    assert.deepEqual(
      inspected.commands.filter((command) => command.source === "skill").map((command) => command.name).sort(),
      ["skill:manage-company", "skill:manage-delivery-domain"],
    );
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
