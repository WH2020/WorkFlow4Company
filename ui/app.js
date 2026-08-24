const state = {
  data: null,
  sessionToken: "",
  activeView: "overview",
  taskFilter: "all",
  library: {
    items: [],
    statistics: { items: {}, categories: {}, versions: 0, fts_available: false },
    filter: "all",
    query: "",
    sort: "updated",
    selectedId: "",
    selectedItem: null,
    dialogItemId: "",
    searchTimer: null,
    requestSequence: 0,
    detailSequence: 0,
  },
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
  "library.item.imported": "资料已导入",
  "library.version.added": "资料版本已新增",
  "library.current_version.changed": "当前版本已切换",
  "library.item.updated": "资料信息已更新",
  "library.item.archived": "资料已归档",
  "library.item.restored": "资料已恢复",
};

const capabilitySymbols = {
  "platform.knowledge": "知",
  "platform.project-space": "项",
  "platform.files": "文",
  "platform.presentation": "演",
  "platform.scheduler": "时",
  "platform.search": "搜",
  "platform.model-gateway": "模",
  "platform.library": "资",
};

const libraryCategoryLabels = {
  bp: "BP",
  patent: "专利",
  development: "开发文档",
  general: "公司通用",
};

const libraryConfidentialityLabels = {
  internal: "内部",
  confidential: "保密",
  highly_confidential: "高度保密",
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
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (state.sessionToken) headers["X-Company-Session"] = state.sessionToken;
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
  if (window.location.hash !== `#${viewId}`) history.replaceState(null, "", `#${viewId}`);
  if (viewId === "knowledge" && state.sessionToken) {
    loadLibrary().catch((error) => toast(error.message, "error"));
  }
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

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderLibraryStatistics(statistics = state.library.statistics) {
  const active = statistics.items?.current || 0;
  const categories = statistics.categories || {};
  $("#libraryTotalCount").textContent = active;
  $("#libraryBpCount").textContent = categories.bp || 0;
  $("#libraryPatentCount").textContent = categories.patent || 0;
  $("#libraryDevelopmentCount").textContent = categories.development || 0;
  $("#libraryFilterAllCount").textContent = active;
  $("#libraryFilterBpCount").textContent = categories.bp || 0;
  $("#libraryFilterPatentCount").textContent = categories.patent || 0;
  $("#libraryFilterDevelopmentCount").textContent = categories.development || 0;
  $("#libraryFilterGeneralCount").textContent = categories.general || 0;
  $("#libraryFilterArchivedCount").textContent = statistics.items?.archived || 0;
}

function renderLibraryList() {
  const container = $("#libraryList");
  if (!state.library.items.length) {
    const searching = Boolean(state.library.query);
    container.innerHTML = `
      <div class="library-empty-list">
        <span>${searching ? "⌕" : "资"}</span>
        <strong>${searching ? "没有找到匹配资料" : state.library.filter === "archived" ? "没有已归档资料" : "资料库还是空的"}</strong>
        <p>${searching ? "可以缩短关键词，或切换到其他资料分类。" : "导入一份 BP、专利或开发文档，系统会保留不可变版本和本地证据索引。"}</p>
      </div>`;
    return;
  }
  container.innerHTML = state.library.items.map((item) => {
    const version = item.current_version || {};
    const match = item.match;
    return `
      <button class="library-item ${item.item_id === state.library.selectedId ? "is-active" : ""}" data-library-item-id="${escapeHtml(item.item_id)}" type="button">
        <span class="library-file-icon ${escapeHtml(item.category)}">${escapeHtml((version.extension || ".doc").slice(1, 5))}</span>
        <span class="library-item-copy">
          <strong>${escapeHtml(item.title)}</strong>
          <small>${escapeHtml(match?.snippet || version.original_filename || "暂无文件信息")}</small>
        </span>
        <span class="library-item-meta">
          <span class="confidentiality-badge ${escapeHtml(item.confidentiality)}">${escapeHtml(libraryConfidentialityLabels[item.confidentiality] || item.confidentiality)}</span>
          <small>v${escapeHtml(version.version_number || "—")} · ${escapeHtml(formatTime(item.updated_at))}</small>
        </span>
      </button>`;
  }).join("");
}

function renderLibraryDetail() {
  const container = $("#libraryDetail");
  const item = state.library.selectedItem;
  if (!item) {
    container.innerHTML = `<div class="library-detail-empty"><span>资</span><h3>选择一份资料</h3><p>这里会显示当前版本、正文预览、来源位置和历史版本。</p></div>`;
    return;
  }
  const current = item.current_version || {};
  const tags = item.tags?.length
    ? item.tags.map((tag) => `<span class="mini-tag">${escapeHtml(tag)}</span>`).join("")
    : `<span class="mini-tag">暂无标签</span>`;
  const preview = current.preview
    || current.extraction_error
    || "当前版本没有可预览文字，可下载原文件查看。";
  container.innerHTML = `
    <div class="library-detail-head">
      <div class="library-detail-head-top">
        <div><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(libraryCategoryLabels[item.category] || item.category)} · ${escapeHtml(current.original_filename || "—")}</p></div>
        <span class="confidentiality-badge ${escapeHtml(item.confidentiality)}">${escapeHtml(libraryConfidentialityLabels[item.confidentiality] || item.confidentiality)}</span>
      </div>
      <div class="library-actions">
        <button class="primary-inline" data-library-download-version="${escapeHtml(current.version_id)}" data-library-filename="${escapeHtml(current.original_filename)}" type="button">下载当前版本</button>
        <button data-library-new-version="${escapeHtml(item.item_id)}" type="button" ${item.status === "archived" ? "disabled" : ""}>上传新版本</button>
        <button data-library-status-action="${item.status === "archived" ? "restore" : "archive"}" data-library-item="${escapeHtml(item.item_id)}" type="button">${item.status === "archived" ? "恢复资料" : "归档资料"}</button>
      </div>
    </div>
    <div class="library-detail-scroll">
      <div class="library-meta-grid">
        <div><span>当前版本</span><strong>v${escapeHtml(current.version_number || "—")}</strong></div>
        <div><span>文件大小</span><strong>${escapeHtml(formatBytes(current.size_bytes))}</strong></div>
        <div><span>正文索引</span><strong>${current.extraction_status === "ready" ? "可检索" : "仅保存原文件"}</strong></div>
        <div><span>共计版本</span><strong>${escapeHtml(item.versions.length)}</strong></div>
      </div>
      <div class="tag-row">${tags}</div>
      <h4 class="library-section-title">当前版本预览</h4>
      <pre class="library-preview">${escapeHtml(preview)}</pre>
      <p class="library-evidence">来源：${escapeHtml(current.original_filename || "—")} · v${escapeHtml(current.version_number || "—")} · SHA-256 ${escapeHtml((current.content_sha256 || "").slice(0, 16))}…${current.preview_truncated ? " · 预览已截断" : ""}</p>
      <h4 class="library-section-title">版本历史</h4>
      <div class="library-versions">
        ${item.versions.map((version) => `
          <div class="library-version">
            <span>v${escapeHtml(version.version_number)}</span>
            <div><strong>${escapeHtml(version.original_filename)}</strong><small>${escapeHtml(version.version_note || "无版本说明")} · ${escapeHtml(formatTime(version.created_at))}</small></div>
            <div class="library-version-buttons">
              <button data-library-download-version="${escapeHtml(version.version_id)}" data-library-filename="${escapeHtml(version.original_filename)}" type="button">下载</button>
              <button data-library-current-version="${escapeHtml(version.version_id)}" data-library-item="${escapeHtml(item.item_id)}" type="button" ${version.version_id === item.current_version_id ? "disabled" : ""}>${version.version_id === item.current_version_id ? "当前" : "设为当前"}</button>
            </div>
          </div>`).join("")}
      </div>
    </div>`;
}

async function selectLibraryItem(itemId) {
  const detailSequence = ++state.library.detailSequence;
  state.library.selectedId = itemId;
  renderLibraryList();
  const requestedId = itemId;
  const result = await api(`/api/library/items/${encodeURIComponent(itemId)}`);
  if (state.library.selectedId !== requestedId || detailSequence !== state.library.detailSequence) return;
  state.library.selectedItem = result.item;
  renderLibraryDetail();
}

async function loadLibrary() {
  const sequence = ++state.library.requestSequence;
  state.library.detailSequence += 1;
  const params = new URLSearchParams();
  if (state.library.filter === "archived") params.set("status", "archived");
  else if (state.library.filter !== "all") params.set("category", state.library.filter);
  if (state.library.query) params.set("q", state.library.query);
  params.set("sort", state.library.sort);
  const payload = await api(`/api/library?${params}`);
  if (sequence !== state.library.requestSequence) return;
  state.library.items = payload.items;
  state.library.statistics = payload.statistics;
  renderLibraryStatistics();
  const resultLabel = state.library.query
    ? `找到 ${payload.items.length} 份与“${state.library.query}”相关的资料`
    : `共 ${payload.items.length} 份${state.library.filter === "archived" ? "已归档" : "当前"}资料`;
  $("#libraryResultSummary").textContent = resultLabel;
  const selectedStillVisible = payload.items.some((item) => item.item_id === state.library.selectedId);
  if (!selectedStillVisible) {
    state.library.selectedId = payload.items[0]?.item_id || "";
    state.library.selectedItem = null;
  }
  renderLibraryList();
  if (state.library.selectedId) await selectLibraryItem(state.library.selectedId);
  else renderLibraryDetail();
}

function setLibraryFilter(filter) {
  state.library.filter = filter;
  $$("[data-library-filter]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.libraryFilter === filter);
  });
  loadLibrary().catch((error) => toast(error.message, "error"));
}

function openLibraryDialog(itemId = "") {
  state.library.dialogItemId = itemId;
  const isNewVersion = Boolean(itemId);
  $("#libraryDialogTitle").textContent = isNewVersion ? "上传新版本" : "导入资料";
  $("#librarySubmitButton").textContent = isNewVersion ? "上传并设为当前版本" : "导入并建立版本";
  $("#libraryFilePrompt").textContent = isNewVersion ? "选择新版本文件" : "选择文件";
  $("#libraryFile").value = "";
  $("#libraryTitle").value = "";
  $("#libraryTags").value = "";
  $("#libraryNote").value = "";
  $("#libraryCategory").value = "bp";
  $("#libraryConfidentiality").value = "confidential";
  $("#libraryTitle").closest(".field").hidden = isNewVersion;
  $("#libraryCategory").closest(".field-grid").hidden = isNewVersion;
  $("#libraryTags").closest(".field").hidden = isNewVersion;
  $("#libraryNote").placeholder = isNewVersion ? "例如：更新市场规模与财务预测" : "例如：首次归档";
  $("#libraryDialog").showModal();
}

async function submitLibraryDocument(event) {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    $("#libraryDialog").close();
    return;
  }
  const file = $("#libraryFile").files[0];
  if (!file) {
    toast("请先选择资料文件", "error");
    return;
  }
  if (file.size > 50 * 1024 * 1024) {
    toast("单个资料文件不能超过 50 MB", "error");
    return;
  }
  if (state.library.dialogItemId && !window.confirm("上传后会把这个版本设为当前版本，并保留原有版本。是否继续？")) return;
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("version_note", $("#libraryNote").value.trim());
  if (state.library.dialogItemId) {
    form.append("item_id", state.library.dialogItemId);
    form.append("make_current", "true");
    form.append("owner_confirmed", "true");
  } else {
    form.append("title", $("#libraryTitle").value.trim());
    form.append("category", $("#libraryCategory").value);
    form.append("confidentiality", $("#libraryConfidentiality").value);
    form.append("tags", $("#libraryTags").value.trim());
  }
  const button = $("#librarySubmitButton");
  button.disabled = true;
  button.textContent = "正在安全导入…";
  try {
    const result = await api("/api/library/import", { method: "POST", body: form });
    $("#libraryDialog").close();
    state.library.detailSequence += 1;
    state.library.selectedId = result.item.item_id;
    state.library.selectedItem = result.item;
    state.library.filter = "all";
    $("#librarySearchInput").value = "";
    state.library.query = "";
    setLibraryFilter("all");
    toast(state.library.dialogItemId ? "新版本已保存并设为当前版本" : "资料已安全导入本机资料库");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = state.library.dialogItemId ? "上传并设为当前版本" : "导入并建立版本";
  }
}

async function downloadLibraryVersion(versionId, filename) {
  const response = await fetch(`/api/library/versions/${encodeURIComponent(versionId)}/content`, {
    headers: { "X-Company-Session": state.sessionToken },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.message || "资料下载失败");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "公司资料";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function setCurrentLibraryVersion(itemId, versionId) {
  if (!window.confirm("要把这个历史版本重新设为当前版本吗？原有版本不会被删除。")) return;
  await api(`/api/library/items/${encodeURIComponent(itemId)}/current`, {
    method: "POST",
    body: JSON.stringify({ version_id: versionId, owner_confirmed: true }),
  });
  state.library.detailSequence += 1;
  toast("当前版本已切换");
  await loadLibrary();
}

async function changeLibraryStatus(itemId, action) {
  const archiving = action === "archive";
  const confirmed = window.confirm(
    archiving
      ? "归档后，资料会从默认列表移到“已归档”，历史版本仍会保留。是否继续？"
      : "要把这份资料恢复到当前资料列表吗？",
  );
  if (!confirmed) return;
  await api(`/api/library/items/${encodeURIComponent(itemId)}/${action}`, {
    method: "POST",
    body: JSON.stringify({ owner_confirmed: true }),
  });
  state.library.detailSequence += 1;
  state.library.selectedId = "";
  state.library.selectedItem = null;
  toast(archiving ? "资料已归档" : "资料已恢复");
  await loadLibrary();
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
  state.library.statistics = state.data.library || state.library.statistics;
  renderLibraryStatistics();
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
    if (state.activeView === "knowledge") await loadLibrary();
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
  $("#libraryImportButton").addEventListener("click", () => openLibraryDialog());
  $("#libraryForm").addEventListener("submit", submitLibraryDocument);
  $("#libraryFile").addEventListener("change", () => {
    const file = $("#libraryFile").files[0];
    $("#libraryFilePrompt").textContent = file ? file.name : "选择文件";
  });
  $("#libraryCategory").addEventListener("change", () => {
    const defaults = { bp: "confidential", patent: "highly_confidential", development: "internal", general: "internal" };
    $("#libraryConfidentiality").value = defaults[$("#libraryCategory").value] || "internal";
  });
  $$("[data-library-filter]").forEach((button) => button.addEventListener("click", () => {
    setLibraryFilter(button.dataset.libraryFilter);
  }));
  $("#librarySearchInput").addEventListener("input", () => {
    window.clearTimeout(state.library.searchTimer);
    state.library.searchTimer = window.setTimeout(() => {
      state.library.query = $("#librarySearchInput").value.trim();
      loadLibrary().catch((error) => toast(error.message, "error"));
    }, 260);
  });
  $("#librarySortSelect").addEventListener("change", () => {
    state.library.sort = $("#librarySortSelect").value;
    loadLibrary().catch((error) => toast(error.message, "error"));
  });
  $("#globalSearchInput").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    state.library.query = $("#globalSearchInput").value.trim();
    $("#librarySearchInput").value = state.library.query;
    switchView("knowledge");
  });
  $("#libraryList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-library-item-id]");
    if (!button) return;
    selectLibraryItem(button.dataset.libraryItemId).catch((error) => toast(error.message, "error"));
  });
  $("#libraryDetail").addEventListener("click", (event) => {
    const download = event.target.closest("[data-library-download-version]");
    if (download) {
      downloadLibraryVersion(download.dataset.libraryDownloadVersion, download.dataset.libraryFilename)
        .catch((error) => toast(error.message, "error"));
      return;
    }
    const newVersion = event.target.closest("[data-library-new-version]");
    if (newVersion) {
      openLibraryDialog(newVersion.dataset.libraryNewVersion);
      return;
    }
    const current = event.target.closest("[data-library-current-version]");
    if (current) {
      setCurrentLibraryVersion(current.dataset.libraryItem, current.dataset.libraryCurrentVersion)
        .catch((error) => toast(error.message, "error"));
      return;
    }
    const status = event.target.closest("[data-library-status-action]");
    if (status) {
      changeLibraryStatus(status.dataset.libraryItem, status.dataset.libraryStatusAction)
        .catch((error) => toast(error.message, "error"));
    }
  });
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
    const requestedView = window.location.hash.slice(1);
    if ($(`#view-${requestedView}`)) switchView(requestedView);
  } catch {
    $("#view-overview").innerHTML = `<div class="empty-feature panel"><div class="empty-illustration"><span>!</span></div><h2>工作台暂时无法加载</h2><p>请关闭应用后重新启动；若问题持续，请运行安装说明中的自检命令。</p></div>`;
  } finally {
    $("#loadingCover").classList.add("is-hidden");
  }
}

boot();
