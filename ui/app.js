const state = {
  data: null,
  sessionToken: "",
  activeView: "overview",
  taskFilter: "all",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const statusLabels = {
  running: "运行中",
  waiting_approval: "待审批",
  completed: "已完成",
  rejected: "已驳回",
  failed: "失败",
  cancelled: "已取消",
  pending: "待处理",
};

const actionLabels = {
  "task.created": "任务已创建",
  "node.completed": "工作流节点已完成",
  "approval.requested": "已发起审批",
  "approval.decided": "审批已处理",
  "task.completed": "任务已完成",
};

const capabilitySymbols = {
  "platform.knowledge": "知",
  "platform.project-space": "项",
  "platform.files": "文",
  "platform.presentation": "演",
  "platform.scheduler": "时",
  "platform.search": "搜",
  "platform.model-gateway": "模",
};

const capabilityModeLabels = {
  unconfigured: { label: "待配置", className: "pending" },
  "disabled-until-configured": { label: "默认关闭", className: "pending" },
  "local-only": { label: "本地模式", className: "pending" },
  "local-empty": { label: "本地空环境", className: "pending" },
  "adapter-ready": { label: "契约已接入", className: "pending" },
  "built-in": { label: "已启用", className: "ready" },
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function shortId(value) {
  return value ? String(value).slice(0, 8) : "—";
}

function domainForId(domainId) {
  return state.data?.business_domains?.find((domain) => domain.id === domainId);
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-Company-Session"] = state.sessionToken;
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({ message: "工作台返回了无法识别的响应" }));
  if (!response.ok) throw new Error(payload.message || `请求失败（${response.status}）`);
  return payload;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  $("#toastRegion").append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function switchView(viewId) {
  const target = $(`#view-${viewId}`);
  if (!target) return;
  state.activeView = viewId;
  $$(".view").forEach((view) => view.classList.toggle("is-visible", view === target));
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === viewId));
  $("#pageTitle").textContent = target.dataset.title || "公司管理平台";
  $("#pageEyebrow").textContent = target.dataset.eyebrow || "统一工作台";
  $("#contentScroll").scrollTop = 0;
}

function renderMetrics() {
  const metrics = state.data.metrics;
  $("#metricRunning").textContent = metrics.running_tasks;
  $("#metricApprovals").textContent = metrics.pending_approvals;
  $("#metricCompleted").textContent = metrics.completed_tasks;
  $("#metricDomains").textContent = metrics.enabled_domains;
  $("#taskNavCount").textContent = state.data.tasks.length;
  $("#approvalNavCount").textContent = metrics.pending_approvals;
}

function renderAttention() {
  const approvals = state.data.approvals;
  const running = state.data.tasks.filter((task) => task.status !== "completed");
  const items = [
    ...approvals.map((approval) => ({
      symbol: "审",
      title: approval.payload?.title || "待处理审批",
      meta: `${domainForId(approval.domain_id)?.display_name || approval.domain_id} · ${formatTime(approval.created_at)}`,
      status: "待审批",
      className: "waiting",
      view: "approvals",
    })),
    ...running.filter((task) => !approvals.some((approval) => approval.task_id === task.task_id)).map((task) => ({
      symbol: "任",
      title: task.title,
      meta: `${task.domain_id} · ${formatTime(task.updated_at)}`,
      status: statusLabels[task.status] || task.status,
      className: task.status,
      view: "tasks",
    })),
  ].slice(0, 4);
  $("#attentionList").innerHTML = items.length
    ? items.map((item) => `
      <button class="attention-item" data-view="${escapeHtml(item.view)}" type="button">
        <span class="attention-symbol">${escapeHtml(item.symbol)}</span>
        <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.meta)}</small></span>
        <span class="status-pill ${escapeHtml(item.className)}">${escapeHtml(item.status)}</span>
      </button>`).join("")
    : `<div class="empty-inline"><div><span>✓</span><strong>目前没有需要立即处理的事项</strong><p>${state.data.workflows.length ? "可发起一个业务域任务，验证受控执行流程。" : "当前组合未启用业务域，公司核心仍可独立运行。"}</p></div></div>`;
}

function renderNotices() {
  $("#noticePanel").innerHTML = state.data.notices
    .map((notice) => `<div class="notice-item">${escapeHtml(notice)}</div>`)
    .join("");
}

function renderTasks() {
  const tasks = state.data.tasks.filter(
    (task) => state.taskFilter === "all" || task.status === state.taskFilter,
  );
  const container = $("#taskList");
  if (!tasks.length) {
    container.innerHTML = `
      <div class="list-empty"><div><span class="list-empty-symbol">任</span><h3>这里还没有对应任务</h3><p>业务任务会在统一 DAG 中推进，并与审批和审计保持同一个事实来源。</p></div></div>`;
    return;
  }
  container.innerHTML = tasks.map((task) => `
    <article class="task-row">
      <div class="task-main"><span class="task-domain">${escapeHtml((domainForId(task.domain_id)?.display_name || "域").slice(0, 1))}</span><span><strong>${escapeHtml(task.title)}</strong><small>任务 ${escapeHtml(shortId(task.task_id))} · ${escapeHtml(task.workflow_id)}</small></span></div>
      <div class="task-cell"><strong>${escapeHtml(domainForId(task.domain_id)?.display_name || task.domain_id)}</strong><small>${escapeHtml(task.project_id || "未关联项目")}</small></div>
      <div><span class="status-pill ${escapeHtml(task.status)}">${escapeHtml(statusLabels[task.status] || task.status)}</span></div>
      <div class="task-cell"><strong>${escapeHtml(formatTime(task.updated_at))}</strong><small>版本 ${escapeHtml(task.version)}</small></div>
    </article>`).join("");
}

function renderApprovals() {
  const container = $("#approvalList");
  const approvals = state.data.approvals;
  if (!approvals.length) {
    container.innerHTML = `
      <div class="list-empty"><div><span class="list-empty-symbol">✓</span><h3>没有待处理审批</h3><p>需要写入业务数据的任务会自动停在这里，未经确认不会继续。</p></div></div>`;
    return;
  }
  container.innerHTML = approvals.map((approval) => `
    <article class="approval-card">
      <div class="approval-card-head">
        <div><h3>${escapeHtml(approval.payload?.title || "业务域写入申请")}</h3><div class="approval-meta"><span>${escapeHtml(domainForId(approval.domain_id)?.display_name || approval.domain_id)}</span><span>策略 ${escapeHtml(approval.policy_id)}</span><span>版本 ${escapeHtml(approval.expected_version)}</span><span>${escapeHtml(formatTime(approval.created_at))}</span></div></div>
        <span class="status-pill waiting">待审批</span>
      </div>
      <div class="approval-payload"><strong>拟执行内容</strong><p>${escapeHtml(approval.payload?.proposed_change || "记录业务行动意图")}</p><p class="hash">SHA-256 · ${escapeHtml(approval.payload_sha256)}</p></div>
      <div class="approval-actions"><button class="danger-button" data-approval-id="${escapeHtml(approval.approval_id)}" data-decision="rejected" type="button">驳回</button><button class="primary-button" data-approval-id="${escapeHtml(approval.approval_id)}" data-decision="approved" type="button">确认并继续</button></div>
    </article>`).join("");
}

function renderCapabilities() {
  $("#capabilityGrid").innerHTML = state.data.platform_capabilities.map((capability) => {
    const status = capabilityModeLabels[capability.configuration_mode]
      || (capability.requires_user_configuration
        ? capabilityModeLabels.unconfigured
        : capabilityModeLabels["adapter-ready"]);
    return `
      <article class="capability-card panel">
        <div class="capability-top"><span class="capability-symbol">${escapeHtml(capabilitySymbols[capability.id] || "能")}</span><span class="status-pill ${escapeHtml(status.className)}">${escapeHtml(status.label)}</span></div>
        <h3>${escapeHtml(capability.display_name)}</h3>
        <p>${escapeHtml(capability.description)}</p>
        <div class="tag-row">${capability.capabilities.slice(0, 3).map((item) => `<span class="mini-tag">${escapeHtml(item.split(".").at(-1))}</span>`).join("")}</div>
      </article>`;
  }).join("");
}

function renderDomains() {
  const domains = state.data.business_domains;
  $("#domainGrid").innerHTML = domains.map((domain) => {
    const workflow = state.data.workflows.find((item) => item.plugin === domain.id);
    return `
      <article class="domain-card panel">
        <div class="domain-card-header"><div class="domain-identity"><span class="domain-logo">${escapeHtml(domain.display_name.slice(0, 1))}</span><div><h3>${escapeHtml(domain.display_name)}</h3><p class="domain-version">${escapeHtml(domain.id)} · v${escapeHtml(domain.version)}</p></div></div><span class="status-pill ${domain.enabled ? "ready" : "pending"}">${domain.enabled ? "已启用" : "可用 · 未启用"}</span></div>
        <p>${escapeHtml(domain.description)}</p>
        <div class="tag-row">${domain.capabilities.map((item) => `<span class="mini-tag">${escapeHtml(item)}</span>`).join("")}</div>
        <div class="domain-actions"><span>${domain.enabled ? "通过插件贡献流程与导航" : "切换到启用该域的组合后可发起任务"}</span><button class="primary-button" data-workflow-id="${escapeHtml(workflow?.id || "")}" type="button" ${workflow ? "" : "disabled"}>${workflow ? "发起域任务" : "当前未启用"}</button></div>
      </article>`;
  }).join("") || `<div class="list-empty panel"><div><span class="list-empty-symbol">域</span><h3>尚未安装业务域</h3><p>公司平台核心仍可独立运行。</p></div></div>`;
}

function renderAudit() {
  const events = state.data.audit;
  const container = $("#auditList");
  if (!events.length) {
    container.innerHTML = `<div class="list-empty"><div><span class="list-empty-symbol">迹</span><h3>尚无审计事件</h3><p>发起任务后，创建、节点、审批和完成事件会显示在这里。</p></div></div>`;
    return;
  }
  container.innerHTML = events.map((event) => `
    <div class="audit-row">
      <span class="audit-symbol">迹</span>
      <div class="audit-event"><strong>${escapeHtml(actionLabels[event.action] || event.action)}</strong><small>${escapeHtml(event.domain_id || "平台核心")} · ${escapeHtml(event.result)}</small></div>
      <div class="audit-cell">${escapeHtml(event.actor_id)}<br><small>${escapeHtml(event.actor_role)}</small></div>
      <div class="audit-cell">${escapeHtml(formatTime(event.created_at))}<br><code>${escapeHtml(shortId(event.task_id))}</code></div>
    </div>`).join("");
}

function populateWorkflowSelect() {
  const select = $("#workflowSelect");
  select.innerHTML = state.data.workflows.map((workflow) => `<option value="${escapeHtml(workflow.id)}">${escapeHtml(workflow.display_name)}</option>`).join("");
  const firstWorkflow = state.data.workflows[0];
  $("#domainWorkflowName").textContent = firstWorkflow?.display_name || "业务域工作";
  $("#domainWorkflowHint").textContent = firstWorkflow ? "首个业务域验证流程" : "尚无可用工作流";
  updateWorkflowDescription();
}

function renderWorkflowAvailability() {
  const enabled = state.data.workflows.length > 0;
  $$("#newTaskButton, #heroTaskButton, [data-open-task], #domainWorkflowQuickAction").forEach((button) => {
    button.disabled = !enabled;
    button.title = enabled ? "" : "当前组合未启用可发起的业务域工作流";
  });
}

function updateWorkflowDescription() {
  const workflow = state.data.workflows.find((item) => item.id === $("#workflowSelect").value);
  $("#workflowDescription").textContent = workflow?.description || "选择由业务域提供的受控流程。";
}

function renderAll() {
  renderMetrics();
  renderAttention();
  renderNotices();
  renderTasks();
  renderApprovals();
  renderCapabilities();
  renderDomains();
  renderAudit();
  populateWorkflowSelect();
  renderWorkflowAvailability();
  bindDynamicActions();
}

async function reload({ silent = false } = {}) {
  try {
    const data = await api("/api/bootstrap");
    state.data = data;
    state.sessionToken = data.session_token;
    renderAll();
    if (!silent) toast("工作台已刷新");
  } catch (error) {
    toast(error.message, "error");
    throw error;
  }
}

function openTaskDialog(preferredWorkflowId = "") {
  if (!state.data?.workflows?.length) {
    toast("当前没有可发起的业务工作流", "error");
    return;
  }
  if (preferredWorkflowId) $("#workflowSelect").value = preferredWorkflowId;
  const selected = state.data.workflows.find((workflow) => workflow.id === $("#workflowSelect").value);
  $("#taskTitle").value = preferredWorkflowId && selected ? `本周${selected.display_name}` : "";
  $("#projectId").value = "";
  updateWorkflowDescription();
  $("#taskDialog").showModal();
  window.setTimeout(() => $("#taskTitle").focus(), 30);
}

async function submitTask(event) {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    $("#taskDialog").close();
    return;
  }
  const button = $("#submitTaskButton");
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    const result = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        workflow_id: $("#workflowSelect").value,
        title: $("#taskTitle").value.trim(),
        project_id: $("#projectId").value.trim() || undefined,
      }),
    });
    $("#taskDialog").close();
    toast(result.task.status === "waiting_approval" ? "任务已到达审批中心" : "任务已启动");
    await reload({ silent: true });
    switchView(result.task.status === "waiting_approval" ? "approvals" : "tasks");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "开始任务";
  }
}

async function decideApproval(button) {
  const approved = button.dataset.decision === "approved";
  const message = approved
    ? "确认该载荷后，工作流将继续记录合成行动意图。是否继续？"
    : "驳回后，本次任务将停止。是否继续？";
  if (!window.confirm(message)) return;
  button.disabled = true;
  try {
    await api(`/api/approvals/${button.dataset.approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision: button.dataset.decision, reason: approved ? "本地管理员确认" : "本地管理员驳回" }),
    });
    toast(approved ? "审批已通过，任务执行完成" : "审批已驳回");
    await reload({ silent: true });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function bindDynamicActions() {
  $$('[data-approval-id]').forEach((button) => button.addEventListener("click", () => decideApproval(button)));
  $$('[data-workflow-id]').forEach((button) => button.addEventListener("click", () => openTaskDialog(button.dataset.workflowId)));
  $$('[data-view]').forEach((button) => {
    if (button.dataset.boundView === "true") return;
    button.dataset.boundView = "true";
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
}

function bindStaticActions() {
  $("#newTaskButton").addEventListener("click", () => openTaskDialog());
  $("#heroTaskButton").addEventListener("click", () => openTaskDialog());
  $("#domainWorkflowQuickAction").addEventListener("click", () => openTaskDialog(state.data?.workflows?.[0]?.id || ""));
  $$('[data-open-task]').forEach((button) => button.addEventListener("click", () => openTaskDialog()));
  $("#refreshButton").addEventListener("click", () => reload());
  $("#taskForm").addEventListener("submit", submitTask);
  $("#workflowSelect").addEventListener("change", updateWorkflowDescription);
  $$('[data-task-filter]').forEach((button) => button.addEventListener("click", () => {
    state.taskFilter = button.dataset.taskFilter;
    $$('[data-task-filter]').forEach((item) => item.classList.toggle("is-active", item === button));
    renderTasks();
  }));
  bindDynamicActions();
}

async function boot() {
  bindStaticActions();
  try {
    await reload({ silent: true });
  } catch {
    $("#view-overview").innerHTML = `<div class="empty-feature panel"><div class="empty-illustration"><span>!</span></div><h2>工作台暂时无法加载</h2><p>请关闭应用后重新启动；若问题持续，请运行安装说明中的自检命令。</p></div>`;
  } finally {
    $("#loadingCover").classList.add("is-hidden");
  }
}

boot();
