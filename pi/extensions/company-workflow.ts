import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { bundleSummary, loadCompanyProfile, loadPluginBundle, planWorkflow } from "./platform-core.ts";

function asToolResult(value: unknown) {
  const text = JSON.stringify(value, null, 2);
  return { content: [{ type: "text" as const, text }], details: value };
}

const GOVERNED_TOOLS = [
  "company_capability_catalog",
  "company_plan_workflow",
  "company_check_domain_permissions",
];

export default function companyWorkflowExtension(pi: ExtensionAPI): void {
  const bundle = loadPluginBundle(process.cwd());
  const profile = loadCompanyProfile(
    process.cwd(),
    bundle,
    process.env.AGENT4COMPANY_PROFILE || "company-manager",
  );
  const enabledDomains = new Set(profile.enabled_domains);

  pi.on("session_start", () => {
    pi.setActiveTools(GOVERNED_TOOLS);
  });

  pi.on("tool_call", (event) => {
    if (!GOVERNED_TOOLS.includes(event.toolName)) {
      return { block: true, reason: `工具 ${event.toolName} 不在公司受控只读工具集内` };
    }
    return undefined;
  });

  pi.registerCommand("company-status", {
    description: "显示当前公司工作台 Profile 和已启用业务域",
    handler: async (_args, context) => {
      context.ui.notify(
        `AGENT4COMPANY_STATUS ${JSON.stringify({
          profile_id: profile.id,
          enabled_domains: profile.enabled_domains,
          active_tools: pi.getActiveTools(),
        })}`,
        "info",
      );
    },
  });

  pi.registerTool({
    name: "company_capability_catalog",
    label: "公司能力目录",
    description: "读取公司级共享能力、可插拔业务域和受控工作流目录。该工具不读取业务数据。",
    parameters: Type.Object({}),
    async execute() {
      return asToolResult(bundleSummary(bundle, profile));
    },
  });

  pi.registerTool({
    name: "company_plan_workflow",
    label: "规划受控工作流",
    description: "按插件清单读取指定 DAG 的节点、权限和审批边界，只生成执行计划，不直接写入。",
    parameters: Type.Object({
      workflow_id: Type.String({ minLength: 1, maxLength: 180 }),
    }),
    async execute(_toolCallId, params) {
      const workflow = bundle.workflows.get(params.workflow_id);
      if (!workflow) throw new Error(`未知工作流：${params.workflow_id}`);
      const plugin = bundle.plugins.get(workflow.plugin)!;
      if (plugin.kind === "business-domain" && !enabledDomains.has(plugin.id)) {
        throw new Error(`当前 Profile 未启用业务域：${plugin.id}`);
      }
      return asToolResult({
        workflow_id: workflow.id,
        domain_id: workflow.plugin,
        domain_kind: plugin.kind,
        stages: planWorkflow(workflow).map((stage, index) => ({
          index,
          nodes: stage.map((node) => ({
            id: node.id,
            type: node.type,
            permissions: node.permissions,
            requires_approval: node.type === "approval",
          })),
        })),
        invariant: "结构化写入由统一任务运行时执行，且必须具有一个直接审批前驱。",
      });
    },
  });

  pi.registerTool({
    name: "company_check_domain_permissions",
    label: "检查业务域权限",
    description: "检查目标业务域是否声明了计划使用的权限，不授予权限也不执行操作。",
    parameters: Type.Object({
      plugin_id: Type.String({ minLength: 1, maxLength: 160 }),
      intended_permissions: Type.Array(Type.String({ minLength: 1, maxLength: 120 }), {
        maxItems: 32,
        uniqueItems: true,
      }),
    }),
    async execute(_toolCallId, params) {
      const plugin = bundle.plugins.get(params.plugin_id);
      if (!plugin) throw new Error(`未知插件：${params.plugin_id}`);
      if (plugin.kind === "business-domain" && !enabledDomains.has(plugin.id)) {
        throw new Error(`当前 Profile 未启用业务域：${plugin.id}`);
      }
      const declared = new Set(plugin.permissions);
      const denied = params.intended_permissions.filter((permission) => !declared.has(permission));
      return asToolResult({
        plugin_id: plugin.id,
        allowed: denied.length === 0,
        declared_permissions: plugin.permissions,
        denied_permissions: denied,
      });
    },
  });
}
