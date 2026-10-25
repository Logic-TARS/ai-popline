"use strict";

const THEME_STORAGE_KEY = "ai-pipeline-theme";
const LEGACY_THEME_STORAGE_KEY = "ai-popline-theme";
const FORM_MEMORY_STORAGE_KEY = "ai-pipeline-form-memory-v1";
const LEGACY_FORM_MEMORY_STORAGE_KEY = "ai-popline-form-memory-v1";

applyTheme(readInitialTheme(), false);

const STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已成功",
  failed: "失败",
  blocked: "已阻止",
  partial: "部分成功",
};

const STEP_LABELS = {
  route: "路由",
  source_scan: "素材",
  image: "图像",
  video: "视频",
  archive: "归档",
  upload: "发布",
  complete: "完成",
};

const EVENT_LABELS = {
  job_created: "任务已创建",
  step_started: "步骤已开始",
  route_resolved: "流水线已确定",
  upload_skipped: "已跳过发布",
  progress_updated: "进度已更新",
  job_finished: "任务已结束",
  publish_requested: "发布已提交",
  publish_started: "发布已开始",
  publish_target_finished: "平台发布已结束",
  publish_finished: "发布操作已结束",
};

const state = {
  bootstrap: null,
  jobs: [],
  selectedTaskId: null,
  selectedJobIds: new Set(),
  pendingPublish: null,
  pollTimer: null,
  validationResult: null,
  resumeTaskDialog: false,
  selectedPublishReadiness: null,
  selectedHasActivePublication: false,
  renameJobContext: null,
  jobPublishContext: null,
  restoringTaskForm: false,
};

const elements = {};

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  window.ContentStudio?.initialize({
    request: apiRequest,
    toast: showToast,
    featureEnabled,
    onTaskCreated: async (taskId) => {
      window.ContentStudio.switchView("jobs");
      await loadJobs();
      await selectTask(taskId);
    },
  });
  window.TaskScheduler?.initialize({
    request: apiRequest,
    toast: showToast,
    getPipelines: () => state.bootstrap?.pipelines || [],
    getPublishEnabled: () => Boolean(state.bootstrap?.publish_enabled),
  });
  initialize();
});

function cacheElements() {
  const ids = [
    "login-view", "login-form", "admin-token", "toggle-token", "login-error", "login-button",
    "app-shell", "service-chip", "refresh-button", "logout-button", "readiness-strip",
    "stat-all", "stat-running", "stat-succeeded", "stat-attention", "last-updated",
    "status-filter", "pipeline-filter", "job-search", "select-all-jobs", "delete-selected-jobs", "jobs-body", "jobs-empty",
    "detail-panel", "detail-empty", "detail-content", "create-task-button", "task-dialog",
    "task-form", "pipeline-cards", "task-info-title", "task-info-help", "task-description-label",
    "task-description", "task-topic-label", "task-topic", "pipeline-help",
    "dynamic-fields", "publish-toggle", "publish-help", "publish-targets-wrap",
    "publish-targets", "add-target-button", "advanced-params", "validation-badge",
    "validation-messages", "task-json-preview", "validate-task-button", "submit-task-button",
    "publish-dialog", "publish-confirm-input", "cancel-publish-button", "confirm-publish-button", "publish-target-summary",
    "rename-job-dialog", "rename-job-form", "rename-job-input", "cancel-rename-job", "submit-rename-job",
    "job-publish-dialog", "job-publish-form", "job-publish-alert", "close-job-publish-dialog",
    "job-publish-title", "job-publish-description", "job-publish-tags",
    "job-publish-targets", "job-publish-add-target",
    "job-publish-confirm-input", "job-publish-target-summary", "cancel-job-publish", "submit-job-publish", "toast-region",
  ];
  ids.forEach((id) => { elements[id] = document.getElementById(id); });
}

function bindEvents() {
  document.querySelectorAll(".theme-toggle").forEach((button) => {
    button.addEventListener("click", toggleTheme);
  });
  updateThemeControls();
  elements["login-form"].addEventListener("submit", handleLogin);
  elements["toggle-token"].addEventListener("click", toggleTokenVisibility);
  elements["logout-button"].addEventListener("click", handleLogout);
  elements["refresh-button"].addEventListener("click", () => refreshWorkspace(true));
  elements["create-task-button"].addEventListener("click", openTaskDialog);
  document.querySelectorAll(".close-dialog").forEach((button) => {
    button.addEventListener("click", () => elements["task-dialog"].close());
  });
  elements["task-dialog"].addEventListener("click", closeDialogFromBackdrop);
  elements["publish-dialog"].addEventListener("click", closeDialogFromBackdrop);
  elements["task-form"].addEventListener("submit", handleTaskSubmit);
  elements["task-form"].addEventListener("input", () => {
    rememberCurrentTaskForm();
    invalidatePreview();
  });
  elements["validate-task-button"].addEventListener("click", () => preflightTask(true));
  elements["publish-toggle"].addEventListener("change", handlePublishToggle);
  elements["add-target-button"].addEventListener("click", () => addTargetRow());
  elements["status-filter"].addEventListener("change", renderJobs);
  elements["pipeline-filter"].addEventListener("change", renderJobs);
  elements["job-search"].addEventListener("input", renderJobs);
  elements["select-all-jobs"].addEventListener("change", toggleAllJobSelection);
  elements["delete-selected-jobs"].addEventListener("click", deleteSelectedJobs);
  elements["cancel-publish-button"].addEventListener("click", cancelPublish);
  elements["publish-confirm-input"].addEventListener("input", updatePublishConfirmation);
  elements["confirm-publish-button"].addEventListener("click", confirmPublish);
  elements["rename-job-dialog"].addEventListener("click", closeDialogFromBackdrop);
  elements["cancel-rename-job"].addEventListener("click", closeRenameJobDialog);
  elements["rename-job-form"].addEventListener("submit", submitRenameJob);
  elements["job-publish-dialog"].addEventListener("click", closeDialogFromBackdrop);
  elements["close-job-publish-dialog"].addEventListener("click", closeJobPublishDialog);
  elements["cancel-job-publish"].addEventListener("click", closeJobPublishDialog);
  elements["job-publish-form"].addEventListener("submit", submitDeferredPublication);
  elements["job-publish-confirm-input"].addEventListener("input", updateDeferredPublishConfirmation);
  elements["job-publish-add-target"].addEventListener("click", () => addDeferredTargetRow());
  elements["job-publish-targets"].addEventListener("input", renderDeferredTargetSummary);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshWorkspace(false);
  });
}

function readInitialTheme() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY) ?? localStorage.getItem(LEGACY_THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch (error) {
    // Storage can be unavailable in hardened or private browser contexts.
  }
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme, persist = true) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = resolved;
  updateThemeControls();
  if (!persist) return;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, resolved);
  } catch (error) {
    // The active theme still works for this page when storage is unavailable.
  }
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

function readFormMemory() {
  try {
    const stored = localStorage.getItem(FORM_MEMORY_STORAGE_KEY) ?? localStorage.getItem(LEGACY_FORM_MEMORY_STORAGE_KEY);
    const parsed = stored ? JSON.parse(stored) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (error) {
    return {};
  }
}

function writeFormMemory(memory) {
  try {
    localStorage.setItem(FORM_MEMORY_STORAGE_KEY, JSON.stringify(memory));
  } catch (error) {
    // Form memory is a convenience only; the form still works without storage.
  }
}

function rememberField(scope, name, value) {
  const memory = readFormMemory();
  memory[`${scope}.${name}`] = value;
  writeFormMemory(memory);
}

function rememberedField(scope, name) {
  return readFormMemory()[`${scope}.${name}`];
}

function updateThemeControls() {
  const light = document.documentElement.dataset.theme === "light";
  document.querySelectorAll(".theme-toggle").forEach((button) => {
    const label = light ? "深色模式" : "浅色模式";
    button.setAttribute("aria-label", `切换到${label}`);
    button.setAttribute("title", `切换到${label}`);
    button.setAttribute("aria-pressed", String(light));
    const icon = button.querySelector(".theme-icon");
    const text = button.querySelector(".theme-label");
    if (icon) icon.textContent = light ? "☾" : "☀";
    if (text) text.textContent = label;
  });
}

async function initialize() {
  try {
    await loadBootstrap();
    showApplication();
    await refreshWorkspace(false);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      showLogin();
      return;
    }
    showLogin("无法连接服务，请确认服务地址和访问策略。", false);
    setServiceState(false, "连接失败");
  }
}

async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body && typeof options.body !== "string") {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.body);
  }
  const method = (options.method || "GET").toUpperCase();
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    const csrf = getCookie("ai_pipeline_csrf") || getCookie("ai_popline_csrf");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }

  let response;
  try {
    response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  } catch (error) {
    throw new ApiError("网络连接失败", 0, null);
  }

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = extractErrorMessage(payload, response.status);
    const errorCode = payload?.error?.code || payload?.detail?.code;
    if (response.status === 401 && errorCode === "authentication_required") {
      showLogin("会话已过期，请重新登录。");
    }
    throw new ApiError(message, response.status, payload);
  }
  return payload;
}

function extractErrorMessage(payload, status) {
  if (status === 405) return "后端服务版本较旧或尚未重启，不支持当前操作。请重启 AI Pipeline 服务后刷新页面。";
  if (payload && typeof payload === "object") {
    if (payload.error?.message) return payload.error.message;
    if (typeof payload.detail === "string") return payload.detail;
    if (payload.detail?.code === "recent_authentication_required") {
      return "发布前需要重新验证管理员令牌，请重新登录。";
    }
    if (payload.detail?.message) return payload.detail.message;
    if (payload.detail?.code) return payload.detail.code;
  }
  return `请求失败（HTTP ${status}）`;
}

function getCookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((part) => part.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

function featureEnabled(name) {
  return state.bootstrap?.api_contract_version >= 2 && state.bootstrap?.features?.[name] === true;
}

async function loadBootstrap() {
  state.bootstrap = await apiRequest("/ui/bootstrap");
  populateFilters();
  if (!state.resumeTaskDialog) renderPipelineCards();
  if (!featureEnabled("job_delete")) {
    elements["delete-selected-jobs"].disabled = true;
    elements["delete-selected-jobs"].title = "后端版本较旧，请重启 AI Pipeline 服务";
    setServiceState(false, "需要重启服务");
  } else {
    setServiceState(true, "服务在线");
  }
}

function showLogin(message = "", focus = true) {
  clearTimeout(state.pollTimer);
  if (elements["task-dialog"].open) {
    state.resumeTaskDialog = true;
    elements["task-dialog"].close();
  }
  if (elements["publish-dialog"].open) elements["publish-dialog"].close();
  if (elements["rename-job-dialog"].open) elements["rename-job-dialog"].close();
  if (elements["job-publish-dialog"].open) elements["job-publish-dialog"].close();
  window.TaskScheduler?.closeDialog();
  elements["app-shell"].hidden = true;
  elements["login-view"].hidden = false;
  elements["login-error"].hidden = !message;
  elements["login-error"].textContent = message;
  if (focus) setTimeout(() => elements["admin-token"].focus(), 50);
}

function showApplication() {
  elements["login-view"].hidden = true;
  elements["app-shell"].hidden = false;
  elements["logout-button"].hidden = !state.bootstrap.auth_required;
  window.ContentStudio?.load();
  window.TaskScheduler?.load();
  if (state.resumeTaskDialog) {
    state.resumeTaskDialog = false;
    elements["task-dialog"].showModal();
  }
}

async function handleLogin(event) {
  event.preventDefault();
  const token = elements["admin-token"].value;
  elements["login-button"].disabled = true;
  elements["login-button"].textContent = "正在验证…";
  elements["login-error"].hidden = true;
  try {
    await apiRequest("/auth/login", { method: "POST", body: { token } });
    elements["admin-token"].value = "";
    await loadBootstrap();
    showApplication();
    await refreshWorkspace(false);
    showToast("登录成功", "success");
  } catch (error) {
    elements["login-error"].textContent = error.message;
    elements["login-error"].hidden = false;
  } finally {
    elements["login-button"].disabled = false;
    elements["login-button"].textContent = "安全登录";
  }
}

function toggleTokenVisibility() {
  const input = elements["admin-token"];
  const visible = input.type === "text";
  input.type = visible ? "password" : "text";
  elements["toggle-token"].textContent = visible ? "显示" : "隐藏";
  elements["toggle-token"].setAttribute("aria-label", visible ? "显示令牌" : "隐藏令牌");
}

async function handleLogout() {
  try {
    await apiRequest("/auth/logout", { method: "POST" });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    showLogin();
  }
}

function setServiceState(online, text) {
  const dot = elements["service-chip"].querySelector(".status-dot");
  dot.classList.toggle("offline", !online);
  elements["service-chip"].lastElementChild.textContent = text;
}

async function refreshWorkspace(showFeedback) {
  try {
    await Promise.all([loadReadiness(), loadJobs()]);
    if (state.selectedTaskId) await loadTaskDetail(state.selectedTaskId, false);
    const currentContract = state.bootstrap?.api_contract_version >= 2;
    setServiceState(currentContract, currentContract ? "服务在线" : "需要重启服务");
    if (showFeedback) showToast(currentContract ? "数据已刷新" : "后端版本较旧，请重启 AI Pipeline 服务", currentContract ? "success" : "warning");
  } catch (error) {
    setServiceState(false, "刷新失败");
    if (!(error instanceof ApiError && error.status === 401)) showToast(error.message, "error");
  } finally {
    schedulePolling();
  }
}

async function loadReadiness() {
  const ready = await apiRequest("/ready");
  const dataReady = ready.data_dir_available;
  const profilesReady = ready.profiles_available;
  const defaultsReady = ready.pipeline_defaults_available;
  elements["readiness-strip"].innerHTML = `
    <span class="ready-item"><span class="status-dot ${dataReady ? "" : "offline"}"></span>任务存储 ${dataReady ? "可用" : "不可用"}</span>
    <span class="ready-divider"></span>
    <span class="ready-item"><span class="status-dot ${profilesReady ? "" : "offline"}"></span>内容配置 ${profilesReady ? "可用" : "不可用"}</span>
    <span class="ready-divider"></span>
    <span class="ready-item"><span class="status-dot ${defaultsReady ? "" : "offline"}"></span>默认参数 ${defaultsReady ? "可用" : "不可用"}</span>
    <span class="ready-divider"></span>
    <span class="ready-item">发布控制 ${state.bootstrap.publish_enabled ? "已启用" : "安全关闭"}</span>`;
}

async function loadJobs() {
  const response = await apiRequest("/jobs?limit=100");
  state.jobs = response.jobs || [];
  if (state.selectedTaskId && !state.jobs.some((job) => job.task_id === state.selectedTaskId)) clearTaskDetail();
  renderStats();
  renderJobs();
  elements["last-updated"].textContent = `更新于 ${new Intl.DateTimeFormat("zh-CN", { timeStyle: "short" }).format(new Date())}`;
}

function populateFilters() {
  elements["status-filter"].innerHTML = `<option value="">全部状态</option>${state.bootstrap.statuses
    .map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(STATUS_LABELS[status] || status)}</option>`).join("")}`;
  elements["pipeline-filter"].innerHTML = `<option value="">全部流水线</option>${state.bootstrap.pipelines
    .map((pipeline) => `<option value="${escapeHtml(pipeline.content_type)}">${escapeHtml(pipeline.label)}</option>`).join("")}`;
}

function renderStats() {
  const running = state.jobs.filter((job) =>
    ["queued", "running"].includes(job.status) || job.publication_summary?.active
  ).length;
  const succeeded = state.jobs.filter((job) => ["succeeded", "partial"].includes(job.status)).length;
  const attention = state.jobs.filter((job) =>
    ["failed", "blocked"].includes(job.status) || job.publication_summary?.needs_attention
  ).length;
  elements["stat-all"].textContent = state.jobs.length;
  elements["stat-running"].textContent = running;
  elements["stat-succeeded"].textContent = succeeded;
  elements["stat-attention"].textContent = attention;
}

function jobDisplayName(job) {
  return job?.display_name || job?.task?.description || "未命名任务";
}

function filteredJobs() {
  const status = elements["status-filter"].value;
  const pipeline = elements["pipeline-filter"].value;
  const query = elements["job-search"].value.trim().toLowerCase();
  return state.jobs.filter((job) => {
    const contentType = job.route?.content_type || job.task?.content_type || "";
    const text = `${job.task_id} ${jobDisplayName(job)} ${job.task?.description || ""}`.toLowerCase();
    return (!status || job.status === status) && (!pipeline || contentType === pipeline) && (!query || text.includes(query));
  });
}

function renderJobs() {
  const jobs = filteredJobs();
  state.selectedJobIds.forEach((taskId) => {
    if (!state.jobs.some((job) => job.task_id === taskId)) state.selectedJobIds.delete(taskId);
  });
  elements["jobs-empty"].hidden = jobs.length > 0;
  elements["jobs-body"].innerHTML = jobs.map((job) => {
    const contentType = job.route?.content_type || job.task?.content_type || "unknown";
    const pipeline = pipelineByType(contentType);
    const selected = job.task_id === state.selectedTaskId ? "selected" : "";
    const checked = state.selectedJobIds.has(job.task_id) ? "checked" : "";
    const name = jobDisplayName(job);
    const deleteDisabled = !canDeleteJob(job);
    return `<tr class="${selected}" data-task-id="${escapeHtml(job.task_id)}" tabindex="0" aria-label="查看任务 ${escapeHtml(name)}">
      <td class="select-cell"><input type="checkbox" data-job-check="${escapeHtml(job.task_id)}" ${checked} ${deleteDisabled ? "disabled" : ""} aria-label="选择任务 ${escapeHtml(name)}"></td>
      <td><span class="job-title">${escapeHtml(name)}</span><span class="job-id">${escapeHtml(job.task_id.slice(0, 12))}</span></td>
      <td><span class="pipeline-label">${escapeHtml(pipeline?.label || contentType)}</span></td>
      <td>${statusBadge(job.status)}</td>
      <td>${publicationStatusBadge(job.publication_summary)}</td>
      <td>${escapeHtml(formatRelativeTime(job.updated_at || job.created_at))}</td>
      <td class="row-actions"><button class="row-delete-button icon-only" type="button" data-delete-job="${escapeHtml(job.task_id)}" aria-label="删除任务 ${escapeHtml(name)}" title="${deleteDisabled ? "运行中、发布中或后端版本过旧，不能删除" : "删除任务"}" ${deleteDisabled ? "disabled" : ""}>×</button></td>
    </tr>`;
  }).join("");
  elements["jobs-body"].querySelectorAll("tr").forEach((row) => {
    const select = () => selectTask(row.dataset.taskId);
    row.addEventListener("click", select);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });
  });
  elements["jobs-body"].querySelectorAll("[data-job-check]").forEach((checkbox) => {
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => toggleJobSelection(checkbox.dataset.jobCheck, checkbox.checked));
  });
  elements["jobs-body"].querySelectorAll("[data-delete-job]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteJob(button.dataset.deleteJob);
    });
  });
  updateJobSelectionControls();
}

function canDeleteJob(job) {
  return featureEnabled("job_delete") && !["queued", "running"].includes(job.status) && !job.publication_summary?.active;
}

function selectableJobs() {
  return filteredJobs().filter(canDeleteJob);
}

function toggleJobSelection(taskId, checked) {
  if (!taskId) return;
  if (checked) state.selectedJobIds.add(taskId);
  else state.selectedJobIds.delete(taskId);
  updateJobSelectionControls();
}

function toggleAllJobSelection(event) {
  const jobs = selectableJobs();
  jobs.forEach((job) => {
    if (event.target.checked) state.selectedJobIds.add(job.task_id);
    else state.selectedJobIds.delete(job.task_id);
  });
  renderJobs();
}

function updateJobSelectionControls() {
  const button = elements["delete-selected-jobs"];
  const selectAll = elements["select-all-jobs"];
  if (!button) return;
  const supported = featureEnabled("job_bulk_delete");
  const count = state.selectedJobIds.size;
  button.disabled = !supported || count === 0;
  button.title = supported ? "" : "后端版本较旧，请重启 AI Pipeline 服务";
  button.textContent = count ? `删除选中（${count}）` : "删除选中";

  if (!selectAll) return;
  const jobs = selectableJobs();
  const selectedCount = jobs.filter((job) => state.selectedJobIds.has(job.task_id)).length;
  selectAll.disabled = !supported || jobs.length === 0;
  selectAll.checked = jobs.length > 0 && selectedCount === jobs.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < jobs.length;
}

function updateJobDeleteButton() {
  updateJobSelectionControls();
}

function clearTaskDetail() {
  state.selectedTaskId = null;
  state.selectedPublishReadiness = null;
  state.selectedHasActivePublication = false;
  elements["detail-empty"].hidden = false;
  elements["detail-content"].hidden = true;
  elements["detail-content"].innerHTML = "";
}

async function deleteJob(taskId) {
  const job = state.jobs.find((item) => item.task_id === taskId);
  const label = jobDisplayName(job) || taskId;
  if (!window.confirm(`确定删除任务“${label}”？此操作不能撤销。`)) return;
  const button = elements["jobs-body"].querySelector(`[data-delete-job="${taskId}"]`);
  if (button) button.disabled = true;
  try {
    await apiRequest(`/jobs/${taskId}`, { method: "DELETE" });
    state.selectedJobIds.delete(taskId);
    if (state.selectedTaskId === taskId) clearTaskDetail();
    await loadJobs();
    showToast("任务已删除", "success");
  } catch (error) {
    if (button) button.disabled = false;
    showToast(error.message, "error");
  }
}

async function deleteSelectedJobs() {
  const taskIds = Array.from(state.selectedJobIds);
  if (!taskIds.length) return;
  if (!window.confirm(`确定删除选中的 ${taskIds.length} 个任务？运行中任务会被保留。`)) return;
  const button = elements["delete-selected-jobs"];
  button.disabled = true;
  try {
    const result = await apiRequest("/jobs/batch-delete", { method: "POST", body: { task_ids: taskIds } });
    if (state.selectedTaskId && (result.deleted || []).includes(state.selectedTaskId)) {
      clearTaskDetail();
    }
    state.selectedJobIds = new Set([...(result.blocked || []), ...(result.failed || [])]);
    await loadJobs();
    const blocked = [...(result.blocked || []), ...(result.not_found || []), ...(result.failed || [])].length;
    showToast(blocked ? `已删除 ${result.deleted?.length || 0} 个任务，${blocked} 个未删除` : `已删除 ${result.deleted?.length || 0} 个任务`, blocked ? "warning" : "success");
  } catch (error) {
    updateJobDeleteButton();
    showToast(error.message, "error");
  }
}

async function selectTask(taskId) {
  state.selectedTaskId = taskId;
  renderJobs();
  await loadTaskDetail(taskId, true);
}

async function loadTaskDetail(taskId, showLoading) {
  if (showLoading) {
    elements["detail-empty"].hidden = true;
    elements["detail-content"].hidden = false;
    elements["detail-content"].innerHTML = `<div class="detail-content"><div class="skeleton-line"></div></div>`;
  }
  try {
    const [snapshot, eventResponse, artifactResponse, publishReadiness] = await Promise.all([
      apiRequest(`/status/${taskId}`),
      apiRequest(`/jobs/${taskId}/events?limit=100`),
      apiRequest(`/jobs/${taskId}/artifacts`),
      apiRequest(`/jobs/${taskId}/publish-readiness`),
    ]);
    if (state.selectedTaskId !== taskId) return;
    state.selectedPublishReadiness = publishReadiness;
    state.selectedHasActivePublication = (snapshot.publication_attempts || []).some((item) => ["queued", "running"].includes(item.status));
    renderTaskDetail(snapshot, eventResponse.events || [], artifactResponse.artifacts || [], publishReadiness);
  } catch (error) {
    if (state.selectedTaskId !== taskId) return;
    elements["detail-content"].innerHTML = `<div class="detail-content"><div class="error-box">${escapeHtml(error.message)}</div></div>`;
  }
}

function renderTaskDetail(snapshot, events, artifacts, publishReadiness) {
  const contentType = snapshot.route?.content_type || snapshot.task?.content_type || "unknown";
  const pipeline = pipelineByType(contentType);
  const steps = state.bootstrap.steps || Object.keys(STEP_LABELS);
  const currentIndex = steps.indexOf(snapshot.current_step);
  const terminal = ["succeeded", "partial"].includes(snapshot.status);
  const stepMarkup = steps.map((step, index) => {
    const className = terminal || index < currentIndex ? "done" : index === currentIndex ? "current" : "";
    const marker = terminal || index < currentIndex ? "✓" : String(index + 1);
    return `<span class="step ${className}"><i>${marker}</i><b>${escapeHtml(STEP_LABELS[step] || step)}</b></span>`;
  }).join("");
  const visibleEvents = compactEvents(events);
  const eventMarkup = visibleEvents.length ? visibleEvents.slice().reverse().map((event) => `
    <div class="timeline-item ${escapeHtml(event.event)}"><span class="timeline-dot"></span><div class="timeline-copy">
      <strong>${escapeHtml(EVENT_LABELS[event.event] || event.event)}</strong>
      ${eventDescription(event)}
      <time>${escapeHtml(formatDateTime(event.timestamp))}</time>
    </div></div>`).join("") : `<p class="muted">暂无事件记录。</p>`;
  const progressMarkup = renderJobProgress(snapshot, contentType);
  const artifactMarkup = renderArtifacts(artifacts);
  const validation = snapshot.artifacts?.validation || {};
  const publicationAttempts = snapshot.publication_attempts || [];
  const publicationSummary = snapshot.publication_summary || publishReadiness?.publication_summary;
  const publishControl = renderDeferredPublishControl(publishReadiness, publicationAttempts);
  const publicationOverview = renderPublicationOverview(publicationSummary);
  const deferredPublishHistory = renderPublicationAttempts(publicationAttempts);
  const generationMode = publishReadiness?.content_studio_job
    ? "生成后在任务中心发布"
    : snapshot.task?.publish ? "生成并发布" : "仅本地生成";
  const renameControl = featureEnabled("job_rename")
    ? `<button id="rename-selected-job" class="button ghost compact" type="button">重命名</button>`
    : "";

  elements["detail-empty"].hidden = true;
  elements["detail-content"].hidden = false;
  elements["detail-content"].className = "detail-content";
  elements["detail-content"].innerHTML = `
    <div class="detail-head"><div><p class="eyebrow">${escapeHtml(pipeline?.label || contentType)}</p><h2>${escapeHtml(jobDisplayName(snapshot))}</h2><p>${escapeHtml(snapshot.task_id)}</p></div><div class="detail-head-actions">${renameControl}${statusBadge(snapshot.status)}${publishControl}</div></div>
    ${publicationOverview}
    <section class="run-overview" aria-labelledby="run-overview-title">
      <div class="section-title"><span aria-hidden="true">↗</span><div><h3 id="run-overview-title">运行概览</h3><p>${escapeHtml(STEP_LABELS[snapshot.current_step] || snapshot.current_step || "等待开始")}</p></div></div>
      <div class="step-track" aria-label="任务步骤">${stepMarkup}</div>
      ${progressMarkup}
    </section>
    ${snapshot.error ? `<div class="error-box">${escapeHtml(snapshot.error)}</div>` : ""}
    <section class="detail-section"><div class="section-title"><span aria-hidden="true">◷</span><div><h3>运行信息</h3><p>任务执行与发布设置</p></div></div><div class="meta-grid">
      ${metaItem("当前步骤", STEP_LABELS[snapshot.current_step] || snapshot.current_step || "等待中")}
      ${metaItem("发布模式", generationMode)}
      ${metaItem("开始时间", formatDateTime(snapshot.started_at || snapshot.created_at))}
      ${metaItem("结束时间", snapshot.finished_at ? formatDateTime(snapshot.finished_at) : "尚未结束")}
    </div></section>
    <section class="detail-section"><div class="section-title"><span aria-hidden="true">✓</span><div><h3>验证结果</h3><p>成片技术参数与完整性</p></div></div>${renderValidationSummary(validation)}</section>
    ${publishReadiness?.content_studio_job ? `<section class="detail-section"><div class="section-title"><span aria-hidden="true">↑</span><div><h3>任务中心发布</h3><p>发布独立于视频生成状态，可失败后重试</p></div></div>${deferredPublishHistory}</section>` : ""}
    <section class="detail-section"><div class="section-title"><span aria-hidden="true">◇</span><div><h3>安全产物</h3><p>${artifacts.length} 个可访问文件</p></div></div>${artifactMarkup}</section>
    <section class="detail-section"><div class="section-title"><span aria-hidden="true">⌁</span><div><h3>事件时间线</h3><p>关键状态与进度更新</p></div></div><div class="timeline">${eventMarkup}</div></section>
    <section class="detail-section raw-section"><details class="raw-details"><summary>开发信息 · 查看原始任务快照</summary><pre>${escapeHtml(JSON.stringify(snapshot, null, 2))}</pre></details></section>`;
  elements["detail-content"].querySelector("#rename-selected-job")?.addEventListener("click", () => openRenameJobDialog(snapshot));
  elements["detail-content"].querySelector("#open-job-publish")?.addEventListener("click", () => openJobPublishDialog(snapshot, publishReadiness));
  elements["detail-content"].querySelectorAll("[data-retry-publication]").forEach((button) => {
    const attempt = publicationAttempts.find((item) => item.attempt_id === button.dataset.retryPublication);
    button.addEventListener("click", () => openJobPublishDialog(snapshot, publishReadiness, attempt?.request));
  });
}

function openRenameJobDialog(snapshot) {
  state.renameJobContext = { taskId: snapshot.task_id };
  elements["rename-job-input"].value = jobDisplayName(snapshot);
  elements["rename-job-dialog"].showModal();
  setTimeout(() => elements["rename-job-input"].select(), 50);
}

function closeRenameJobDialog() {
  state.renameJobContext = null;
  elements["rename-job-dialog"].close();
}

async function submitRenameJob(event) {
  event.preventDefault();
  const taskId = state.renameJobContext?.taskId;
  const displayName = elements["rename-job-input"].value.trim();
  if (!taskId || !displayName) return;
  elements["submit-rename-job"].disabled = true;
  try {
    await apiRequest(`/jobs/${taskId}`, { method: "PATCH", body: { display_name: displayName } });
    closeRenameJobDialog();
    await Promise.all([loadJobs(), loadTaskDetail(taskId, false)]);
    showToast("任务名称已更新", "success");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements["submit-rename-job"].disabled = false;
  }
}

function renderDeferredPublishControl(readiness, attempts) {
  if (!readiness?.content_studio_job) return "";
  const active = attempts.some((item) => ["queued", "running"].includes(item.status));
  const reason = readiness.reasons?.[0] || "";
  const disabled = !readiness.eligible || active;
  return `<button id="open-job-publish" class="button ${disabled ? "secondary" : "primary"} compact" type="button" ${disabled ? "disabled" : ""} title="${escapeHtml(reason)}">${active ? "发布进行中" : "设置发布"}</button>`;
}

function renderPublicationOverview(summary) {
  if (!summary) return "";
  const targets = summary.targets || [];
  const targetMarkup = targets.length ? `<div class="publication-summary-targets">${targets.map((target) => {
    const statusText = target.status === "published" ? "已发布" : target.status === "publishing" ? "发布中" : "失败";
    const visibility = publicationVisibilityLabel(target.visibility);
    const details = [visibility, target.proof].filter(Boolean).join(" · ");
    return `<div class="publication-summary-target ${escapeHtml(target.status)}">
      <div><strong>${escapeHtml(platformLabel(target.platform))}</strong><small>${escapeHtml(target.account || "未记录账号")}</small></div>
      <span class="${target.status === "published" ? "result-success" : target.status === "failed" ? "result-failed" : "result-running"}">${escapeHtml(statusText)}</span>
      <p>${escapeHtml(target.error || details || "等待平台返回凭证")}</p>
    </div>`;
  }).join("")}</div>` : "";
  const latestPublished = new Set(targets.filter((target) => target.success).map((target) => `${target.platform}:${target.account}`));
  const historicalPublished = (summary.published_targets || []).filter((target) => !latestPublished.has(`${target.platform}:${target.account}`));
  const historicalMarkup = historicalPublished.length ? `<div class="publication-previous-success"><strong>此前已发布账号</strong><span>${historicalPublished.map((target) => `${escapeHtml(platformLabel(target.platform))} · ${escapeHtml(target.account)}`).join("；")}</span></div>` : "";
  const completed = summary.completed_at ? `<small class="publication-summary-time">完成于 ${escapeHtml(formatDateTime(summary.completed_at))}</small>` : "";
  return `<section class="publication-summary-card state-${escapeHtml(summary.state)}" aria-label="发布状态">
    <header><div><span>发布状态</span><h3>${escapeHtml(summary.label)}</h3></div>${publicationStatusBadge(summary)}</header>
    <p>${escapeHtml(summary.message)}</p>${completed}${historicalMarkup}${targetMarkup}
  </section>`;
}

function renderPublicationAttempts(attempts) {
  if (!attempts.length) {
    const reason = state.selectedPublishReadiness?.reasons?.[0];
    return `<div class="publication-empty"><p>${escapeHtml(reason || "视频通过审查后，可在此设置平台并发布。")}</p></div>`;
  }
  return `<div class="publication-attempts">${[...attempts].reverse().map((attempt, index) => {
    const results = Object.entries(attempt.results || {});
    const requestedTargets = attempt.request?.publish_targets || [];
    const resultMarkup = results.length ? results.map(([platform, result]) => {
      const target = requestedTargets.find((item) => item.platform === platform);
      const evidence = result.error || [
        target?.account,
        publicationVisibilityLabel(result.delivery_status || result.visibility),
        result.publication_proof || result.draft_proof || result.private_visibility_proof,
      ].filter(Boolean).join(" · ");
      return `<div class="publication-target-result"><span>${escapeHtml(platformLabel(platform))}</span><strong class="${result.success ? "result-success" : "result-failed"}">${result.success ? "成功" : "失败"}</strong><small>${escapeHtml(evidence || "平台未返回详细凭证")}</small></div>`;
    }).join("") : `<p class="muted">等待平台返回结果。</p>`;
    const retry = ["failed", "partial"].includes(attempt.status) && state.selectedPublishReadiness?.eligible
      ? `<button class="button ghost compact" type="button" data-retry-publication="${escapeHtml(attempt.attempt_id)}">调整并重试</button>` : "";
    return `<article class="publication-attempt"><header><div><strong>发布尝试 ${attempts.length - index}</strong><small>${escapeHtml(formatDateTime(attempt.requested_at))}</small></div>${statusBadge(attempt.status)}</header><div class="publication-target-results">${resultMarkup}</div>${attempt.error ? `<p class="publication-error">${escapeHtml(attempt.error)}</p>` : ""}${retry}</article>`;
  }).join("")}</div>`;
}

function platformLabel(platform) {
  return { douyin: "抖音", kuaishou: "快手", bilibili: "Bilibili", tencent: "视频号" }[platform] || platform || "未知平台";
}

function publicationVisibilityLabel(visibility) {
  return { private: "仅自己可见", public: "公开可见", draft: "草稿" }[visibility] || visibility || "";
}

function renderJobProgress(snapshot, contentType) {
  const progress = snapshot.progress;
  if (contentType !== "script_video" || !progress) return "";
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  const finishedAt = snapshot.finished_at || new Date().toISOString();
  const duration = formatDuration(snapshot.started_at || snapshot.created_at, finishedAt);
  const estimate = progress.is_estimate ? " · 预计进度" : "";
  return `<div class="job-progress" aria-label="口播视频生成进度">
    <div class="progress-head"><div><strong>${escapeHtml(progress.phase)}</strong><small>${escapeHtml(progress.message)}${estimate}</small></div><b>${percent}%</b></div>
    <progress class="progress-bar" role="progressbar" max="100" value="${percent}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}" aria-valuetext="${escapeHtml(`${progress.phase} ${percent}%`)}">${percent}%</progress>
    <div class="progress-meta"><span>更新于 ${escapeHtml(formatDateTime(progress.updated_at))}</span><span>耗时 ${escapeHtml(duration)}</span></div>
  </div>`;
}

function compactEvents(events) {
  const compacted = [];
  events.forEach((event) => {
    const previous = compacted.at(-1);
    if (event.event === "progress_updated" && previous?.event === "progress_updated" && previous.payload?.phase === event.payload?.phase) {
      compacted[compacted.length - 1] = event;
    } else {
      compacted.push(event);
    }
  });
  return compacted;
}

function eventDescription(event) {
  if (event.event !== "progress_updated" || !event.payload?.message) return "";
  const percent = Math.max(0, Math.min(100, Number(event.payload.percent) || 0));
  return `<small>${escapeHtml(`${event.payload.phase || "进度"} · ${percent}% · ${event.payload.message}`)}</small>`;
}

function formatDuration(start, end) {
  const milliseconds = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "—";
  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 60) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  if (minutes) return `${minutes} 分钟 ${seconds % 60} 秒`;
  return `${seconds} 秒`;
}

function renderValidationSummary(validation) {
  const video = validation?.video;
  const images = validation?.images;
  if (!video && !images) return `<div class="validation-empty"><span aria-hidden="true">○</span><p>暂无媒体验证数据</p></div>`;
  const metrics = [];
  if (video) {
    metrics.push(["时长", formatMediaDuration(video.duration_seconds)]);
    metrics.push(["画面", `${video.width || "—"} × ${video.height || "—"}`]);
    metrics.push(["视频编码", String(video.video_codec || "—").toUpperCase()]);
    metrics.push(["音频编码", String(video.audio_codec || "—").toUpperCase()]);
  }
  if (images) metrics.push(["图像数量", String(images.count ?? images.files?.length ?? "—")]);
  return `<div class="validation-grid">${metrics.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
}

function formatMediaDuration(value) {
  const seconds = Math.round(Number(value) || 0);
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function renderArtifacts(artifacts) {
  if (!artifacts.length) return `<p class="muted">当前没有可预览的任务内产物。</p>`;
  return `<div class="artifact-grid">${artifacts.map((artifact) => {
    const url = escapeHtml(artifact.url);
    const name = escapeHtml(artifact.name);
    let preview = `<span aria-hidden="true">FILE</span>`;
    if (artifact.media_type.startsWith("image/")) preview = `<img src="${url}" alt="${name}" loading="lazy">`;
    if (artifact.media_type.startsWith("video/")) preview = `<video src="${url}" controls preload="metadata" aria-label="${name}"></video>`;
    return `<a class="artifact-card" href="${url}" target="_blank" rel="noopener">
      <div class="artifact-preview">${preview}</div><div class="artifact-info"><strong>${name}</strong><small>${escapeHtml(formatBytes(artifact.size))} · ${escapeHtml(artifact.media_type)}</small></div>
    </a>`;
  }).join("")}</div>`;
}

function schedulePolling() {
  clearTimeout(state.pollTimer);
  if (document.hidden || !state.bootstrap || elements["app-shell"].hidden) return;
  const hasActiveJobs = state.jobs.some((job) =>
    ["queued", "running"].includes(job.status) || job.publication_summary?.active
  );
  const delay = hasActiveJobs || state.selectedHasActivePublication
    ? state.bootstrap.active_refresh_ms
    : state.bootstrap.idle_refresh_ms;
  state.pollTimer = setTimeout(() => refreshWorkspace(false), delay);
}

function renderPipelineCards() {
  elements["pipeline-cards"].innerHTML = state.bootstrap.pipelines.map((pipeline, index) => `
    <button class="pipeline-card ${index === 0 ? "selected" : ""}" type="button" role="radio" aria-checked="${index === 0}" data-content-type="${escapeHtml(pipeline.content_type)}">
      <span class="pipeline-icon" aria-hidden="true">${escapeHtml(pipeline.icon)}</span><span><strong>${escapeHtml(pipeline.label)}</strong><small>${escapeHtml(pipeline.external_tools.join(" + ") || "本地处理")}</small></span>
    </button>`).join("");
  elements["pipeline-cards"].querySelectorAll(".pipeline-card").forEach((card) => {
    card.addEventListener("click", () => selectPipeline(card.dataset.contentType));
  });
  if (state.bootstrap.pipelines.length) selectPipeline(state.bootstrap.pipelines[0].content_type);
}

function selectPipeline(contentType) {
  rememberCurrentTaskForm();
  elements["pipeline-cards"].querySelectorAll(".pipeline-card").forEach((card) => {
    const selected = card.dataset.contentType === contentType;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-checked", String(selected));
  });
  const pipeline = pipelineByType(contentType);
  configureTaskInfoFields(contentType);
  restoreTaskInfo(pipeline);
  renderDynamicFields(pipeline);
  elements["pipeline-help"].textContent = pipeline?.description || "填写流水线参数。";
  configurePublishing(pipeline);
  restorePublishing(pipeline);
  invalidatePreview();
}

function selectedPipeline() {
  return elements["pipeline-cards"].querySelector(".pipeline-card.selected")?.dataset.contentType || "";
}

function pipelineByType(contentType) {
  return state.bootstrap?.pipelines.find((item) => item.content_type === contentType);
}

function taskParamScope(contentType = selectedPipeline()) {
  return `task.${contentType || "unknown"}`;
}

function configureTaskInfoFields(_contentType) {
  elements["task-info-title"].textContent = "任务信息";
  elements["task-info-help"].textContent = "系统会按流水线自动填入，可按需微调。";
  elements["task-description-label"].innerHTML = `<span class="field-label">任务描述<span class="required">*</span></span>`;
  elements["task-description-label"].appendChild(elements["task-description"]);
  elements["task-description"].placeholder = "例如：生成今日 AI 资讯视频";
  elements["task-topic-label"].innerHTML = `<span class="field-label">内容主题</span>`;
  elements["task-topic-label"].appendChild(elements["task-topic"]);
  elements["task-topic"].required = false;
  elements["task-topic"].placeholder = "留空时使用任务描述";
}

function restoreTaskInfo(pipeline) {
  if (!pipeline) return;
  const scope = taskParamScope(pipeline.content_type);
  const defaults = pipeline.task_defaults || {};
  elements["task-description"].value = rememberedField(scope, "task_description") ?? defaults.description ?? "";
  elements["task-topic"].value = rememberedField(scope, "task_topic") ?? defaults.topic ?? "";
}

function renderDynamicFields(pipeline) {
  if (!pipeline) {
    elements["dynamic-fields"].innerHTML = "";
    return;
  }
  elements["dynamic-fields"].innerHTML = pipeline.fields.map((field) => renderField(field, pipeline.content_type)).join("");
}

function renderField(field, contentType) {
  const id = `param-${field.name}`;
  const required = field.required ? "required" : "";
  const requiredLabel = field.required ? `<span class="required">*</span>` : "";
  const labelText = `<span class="field-label">${escapeHtml(field.label)}${requiredLabel}</span>`;
  const help = field.help ? `<small>${escapeHtml(field.help)}</small>` : "";
  const remembered = rememberedField(taskParamScope(contentType), field.name);
  const defaultValue = Array.isArray(field.default) ? field.default.join(", ") : field.default ?? "";
  const value = remembered ?? defaultValue;
  if (field.type === "checkbox") {
    const checked = remembered === undefined ? Boolean(field.default) : Boolean(remembered);
    return `<div class="checkbox-field"><input id="${id}" data-param-name="${escapeHtml(field.name)}" data-param-type="checkbox" type="checkbox" ${checked ? "checked" : ""}><label for="${id}">${labelText}</label></div>`;
  }
  if (field.type === "select") {
    const selectedValue = remembered ?? field.default;
    const options = field.options.map((option) => `<option value="${escapeHtml(option.value)}" ${option.value === selectedValue ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("");
    return `<label class="field">${labelText}<select id="${id}" data-param-name="${escapeHtml(field.name)}" data-param-type="select" ${required}>${options}</select>${help}</label>`;
  }
  if (field.type === "textarea") {
    return `<label class="field span-two">${labelText}<textarea id="${id}" data-param-name="${escapeHtml(field.name)}" data-param-type="textarea" rows="${field.rows || 4}" placeholder="${escapeHtml(field.placeholder || "")}" ${required}>${escapeHtml(String(value))}</textarea>${help}</label>`;
  }
  const htmlType = field.type === "number" ? "number" : field.type === "url" ? "url" : "text";
  const constraints = `${field.minimum !== null ? `min="${field.minimum}"` : ""} ${field.maximum !== null ? `max="${field.maximum}"` : ""} ${field.step !== null ? `step="${field.step}"` : ""}`;
  return `<label class="field">${labelText}<input id="${id}" data-param-name="${escapeHtml(field.name)}" data-param-type="${escapeHtml(field.type)}" type="${htmlType}" value="${escapeHtml(String(value))}" placeholder="${escapeHtml(field.placeholder || "")}" ${constraints} ${required}>${help}</label>`;
}

function configurePublishing(pipeline) {
  const available = Boolean(state.bootstrap.publish_enabled && pipeline?.publish_targets.length);
  elements["publish-toggle"].disabled = !available;
  elements["publish-toggle"].checked = false;
  elements["publish-targets-wrap"].hidden = true;
  elements["publish-targets"].innerHTML = "";
  elements["publish-help"].textContent = available
    ? `支持 ${pipeline.publish_targets.join("、")}；开启后需要二次确认。`
    : state.bootstrap.publish_enabled ? "该流水线不支持发布。" : "服务端 Web 发布当前安全关闭。";
}

function restorePublishing(pipeline) {
  const saved = rememberedField(taskParamScope(pipeline?.content_type), "publish_settings");
  if (!saved || typeof saved !== "object" || elements["publish-toggle"].disabled) return;
  elements["publish-toggle"].checked = Boolean(saved.publish);
  elements["publish-targets-wrap"].hidden = !elements["publish-toggle"].checked;
  if (!elements["publish-toggle"].checked) return;
  const targets = Array.isArray(saved.targets) ? saved.targets : [];
  targets.forEach((target) => addTargetRow(target));
  if (!elements["publish-targets"].children.length) addTargetRow();
}

function handlePublishToggle() {
  const enabled = elements["publish-toggle"].checked;
  elements["publish-targets-wrap"].hidden = !enabled;
  if (enabled && !elements["publish-targets"].children.length) addTargetRow();
  rememberCurrentTaskForm();
  invalidatePreview();
}

function addTargetRow(target = {}) {
  const pipeline = pipelineByType(selectedPipeline());
  const platforms = pipeline?.publish_targets || [];
  if (!platforms.length) return;
  const row = document.createElement("div");
  row.className = "target-row";
  row.innerHTML = `
    <label class="field">平台<select class="target-platform">${platforms.map((platform) => `<option value="${escapeHtml(platform)}" ${platform === target.platform ? "selected" : ""}>${escapeHtml(platform)}</option>`).join("")}</select></label>
    <label class="field">账号<input class="target-account" value="${escapeHtml(target.account || "")}" required></label>
    <label class="field target-tid-wrap">分区 tid<input class="target-tid" type="number" min="1" value="${escapeHtml(target.tid || "")}"></label>
    <label class="field">可见性<select class="target-visibility"><option value="private" ${target.visibility === "public" ? "" : "selected"}>仅自己可见</option><option value="public" ${target.visibility === "public" ? "selected" : ""}>公开可见</option></select></label>
    <button class="icon-button remove-target" type="button" aria-label="删除发布目标">×</button>`;
  const platform = row.querySelector(".target-platform");
  const visibility = row.querySelector(".target-visibility");
  const updateTid = () => { row.querySelector(".target-tid-wrap").hidden = platform.value !== "bilibili"; };
  platform.addEventListener("change", () => { updateTid(); rememberCurrentTaskForm(); invalidatePreview(); });
  visibility.addEventListener("change", () => { rememberCurrentTaskForm(); invalidatePreview(); });
  row.querySelector(".remove-target").addEventListener("click", () => { row.remove(); rememberCurrentTaskForm(); invalidatePreview(); });
  row.querySelectorAll("input").forEach((input) => input.addEventListener("input", () => { rememberCurrentTaskForm(); invalidatePreview(); }));
  updateTid();
  elements["publish-targets"].appendChild(row);
}

function openTaskDialog() {
  state.restoringTaskForm = true;
  elements["task-form"].reset();
  elements["task-description"].value = "";
  elements["task-topic"].value = "";
  elements["advanced-params"].value = rememberedField("task", "advanced_params") || "{}";
  elements["publish-targets"].innerHTML = "";
  state.validationResult = null;
  const first = state.bootstrap.pipelines[0];
  if (first) selectPipeline(first.content_type);
  state.restoringTaskForm = false;
  updateTaskPreview();
  elements["task-dialog"].showModal();
  setTimeout(() => elements["task-description"].focus(), 50);
}

function closeDialogFromBackdrop(event) {
  if (event.target === event.currentTarget) event.currentTarget.close();
}

function collectDynamicParams() {
  const params = {};
  elements["dynamic-fields"].querySelectorAll("[data-param-name]").forEach((input) => {
    const name = input.dataset.paramName;
    const type = input.dataset.paramType;
    if (type === "checkbox") {
      params[name] = input.checked;
      return;
    }
    const raw = input.value.trim();
    if (!raw) return;
    if (type === "number") params[name] = Number(raw);
    else if (type === "tags") params[name] = raw.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
    else params[name] = raw;
  });
  return params;
}

function rememberCurrentTaskForm() {
  if (!elements["task-form"] || state.restoringTaskForm) return;
  rememberAdvancedParams();
  const scope = taskParamScope();
  rememberField(scope, "task_description", elements["task-description"].value);
  rememberField(scope, "task_topic", elements["task-topic"].value);
  elements["dynamic-fields"].querySelectorAll("[data-param-name]").forEach((input) => {
    const value = input.dataset.paramType === "checkbox" ? input.checked : input.value;
    rememberField(scope, input.dataset.paramName, value);
  });
  const targets = [...elements["publish-targets"].querySelectorAll(".target-row")].map((row) => ({
    platform: row.querySelector(".target-platform").value,
    account: row.querySelector(".target-account").value,
    tid: row.querySelector(".target-tid").value,
    visibility: row.querySelector(".target-visibility").value,
  }));
  rememberField(scope, "publish_settings", { publish: elements["publish-toggle"].checked, targets });
}

function rememberAdvancedParams() {
  const raw = elements["advanced-params"].value || "{}";
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) rememberField("task", "advanced_params", raw);
  } catch (error) {
    // Keep the last valid advanced params instead of storing broken JSON.
  }
}

function collectTask(checkValidity = true) {
  if (checkValidity && !elements["task-form"].reportValidity()) throw new Error("请先填写所有必填字段。");
  const params = collectDynamicParams();

  let advanced;
  try {
    advanced = JSON.parse(elements["advanced-params"].value || "{}");
  } catch (error) {
    throw new Error("高级参数不是有效的 JSON。");
  }
  if (!advanced || Array.isArray(advanced) || typeof advanced !== "object") throw new Error("高级参数必须是 JSON 对象。");

  const publish = elements["publish-toggle"].checked;
  const publishTargets = publish ? [...elements["publish-targets"].querySelectorAll(".target-row")].map((row) => {
    const platform = row.querySelector(".target-platform").value;
    const account = row.querySelector(".target-account").value.trim();
    const tidValue = row.querySelector(".target-tid").value;
    if (!account) throw new Error("每个发布目标都必须填写账号。");
    const target = { platform, account, visibility: row.querySelector(".target-visibility").value };
    if (platform === "bilibili") {
      if (!tidValue) throw new Error("Bilibili 发布目标必须填写分区 tid。");
      target.tid = Number(tidValue);
    }
    return target;
  }) : [];

  const contentType = selectedPipeline();
  const description = elements["task-description"].value.trim();
  const topic = elements["task-topic"].value.trim();

  const task = {
    description,
    content_type: contentType,
    topic: topic || null,
    publish,
    publish_targets: publishTargets,
    params: { ...params, ...advanced },
  };
  rememberCurrentTaskForm();
  return task;
}

function invalidatePreview() {
  state.validationResult = null;
  elements["validation-badge"].className = "badge neutral";
  elements["validation-badge"].textContent = "尚未预检";
  elements["validation-messages"].innerHTML = "";
  updateTaskPreview();
}

function updateTaskPreview() {
  try {
    const task = collectTaskWithoutValidity();
    elements["task-json-preview"].textContent = JSON.stringify(task, null, 2);
  } catch (error) {
    elements["task-json-preview"].textContent = `// ${error.message}`;
  }
}

function collectTaskWithoutValidity() {
  return collectTask(false);
}

async function preflightTask(showFeedback) {
  let task;
  try {
    task = collectTask();
  } catch (error) {
    setValidation(false, [error.message]);
    if (showFeedback) showToast(error.message, "error");
    return null;
  }
  elements["validate-task-button"].disabled = true;
  elements["validate-task-button"].textContent = "正在预检…";
  try {
    const result = await apiRequest("/validate-task", { method: "POST", body: task });
    state.validationResult = result;
    elements["task-json-preview"].textContent = JSON.stringify(result.task, null, 2);
    setValidation(true, result.warnings || []);
    if (showFeedback) showToast("任务预检通过", "success");
    return result;
  } catch (error) {
    const errors = error.payload?.detail?.errors?.map((item) => `${item.field || "参数"}：${item.message}`) || [error.message];
    setValidation(false, errors);
    if (showFeedback) showToast("任务预检未通过", "error");
    return null;
  } finally {
    elements["validate-task-button"].disabled = false;
    elements["validate-task-button"].textContent = "预检任务";
  }
}

function setValidation(valid, messages) {
  elements["validation-badge"].className = `badge ${valid ? "succeeded" : "invalid"}`;
  elements["validation-badge"].textContent = valid ? "预检通过" : "需要修正";
  elements["validation-messages"].innerHTML = messages.map((message) => `<div class="validation-message ${valid ? "warning" : "error"}">${escapeHtml(message)}</div>`).join("");
}

async function handleTaskSubmit(event) {
  event.preventDefault();
  const result = await preflightTask(false);
  if (!result) return;
  if (result.task.publish) {
    state.pendingPublish = result;
    renderPublishTargetSummary(elements["publish-target-summary"], result.task.publish_targets || []);
    elements["publish-confirm-input"].value = "";
    elements["confirm-publish-button"].disabled = true;
    elements["publish-dialog"].showModal();
    setTimeout(() => elements["publish-confirm-input"].focus(), 50);
    return;
  }
  await submitValidatedTask(result);
}

function updatePublishConfirmation() {
  elements["confirm-publish-button"].disabled = elements["publish-confirm-input"].value.trim() !== "确认发布";
}

function cancelPublish() {
  state.pendingPublish = null;
  elements["publish-dialog"].close();
}

async function confirmPublish() {
  if (!state.pendingPublish || elements["publish-confirm-input"].value.trim() !== "确认发布") return;
  const result = state.pendingPublish;
  state.pendingPublish = null;
  elements["publish-dialog"].close();
  await submitValidatedTask(result, `PUBLISH:${result.task_fingerprint}`);
}

async function submitValidatedTask(result, confirmation = "") {
  elements["submit-task-button"].disabled = true;
  elements["submit-task-button"].textContent = "正在提交…";
  try {
    const headers = confirmation ? { "X-AI-Pipeline-Publish-Confirmation": confirmation } : {};
    const response = await apiRequest("/run", { method: "POST", headers, body: result.task });
    elements["task-dialog"].close();
    showToast("任务已进入队列", "success");
    await loadJobs();
    await selectTask(response.task_id);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements["submit-task-button"].disabled = false;
    elements["submit-task-button"].textContent = "开始运行";
  }
}

function openJobPublishDialog(snapshot, readiness, request = null) {
  if (!readiness?.eligible) {
    showToast(readiness?.reasons?.[0] || "当前任务不能发布", "error");
    return;
  }
  const defaults = readiness.defaults || {};
  const selectedRequest = request || {};
  const selectedTargets = selectedRequest.publish_targets || [];
  state.jobPublishContext = { taskId: snapshot.task_id };
  elements["job-publish-title"].value = selectedRequest.title || defaults.title || "";
  elements["job-publish-description"].value = selectedRequest.description ?? defaults.description ?? "";
  elements["job-publish-tags"].value = (selectedRequest.tags || defaults.tags || []).join(", ");
  elements["job-publish-targets"].innerHTML = "";
  if (selectedTargets.length) {
    selectedTargets.forEach((target) => addDeferredTargetRow(target.platform, target.account, target.visibility));
  } else {
    addDeferredTargetRow();
  }
  elements["job-publish-confirm-input"].value = "";
  elements["job-publish-alert"].hidden = true;
  elements["job-publish-alert"].textContent = "";
  elements["submit-job-publish"].disabled = true;
  elements["submit-job-publish"].textContent = "确认并发布";
  elements["job-publish-dialog"].showModal();
  setTimeout(() => elements["job-publish-title"].focus(), 50);
}

function closeJobPublishDialog() {
  state.jobPublishContext = null;
  if (elements["job-publish-dialog"].open) elements["job-publish-dialog"].close();
}

const DEFERRED_PLATFORM_HINTS = { douyin: "仅自己可见", kuaishou: "仅自己可见" };

function deferredSupportedPlatforms() {
  return state.selectedPublishReadiness?.supported_platforms?.length
    ? state.selectedPublishReadiness.supported_platforms
    : ["douyin", "kuaishou"];
}

function deferredAccountOptions(platform) {
  return [...(state.selectedPublishReadiness?.account_options?.[platform] || [])];
}

function renderDeferredAccountControl(row, platform, preferredAccount) {
  const slot = row.querySelector(".deferred-account-slot");
  const defaults = state.selectedPublishReadiness?.defaults?.accounts || {};
  const desired = preferredAccount || defaults[platform] || "";
  const options = deferredAccountOptions(platform);
  if (!options.length) {
    slot.innerHTML = `<input class="deferred-target-account" placeholder="发布账号" required>`;
    slot.querySelector(".deferred-target-account").value = desired;
    return;
  }
  if (desired && !options.some((item) => item.account === desired)) {
    options.unshift({ account: desired, status: "", status_label: "" });
  }
  slot.innerHTML = `<select class="deferred-target-account">${options.map((item) => {
    const suffix = item.status && item.status !== "valid" && item.status_label ? `（${item.status_label}）` : "";
    return `<option value="${escapeHtml(item.account)}" ${item.account === desired ? "selected" : ""}>${escapeHtml(item.account + suffix)}</option>`;
  }).join("")}</select>`;
}

function addDeferredTargetRow(platform, account, visibility) {
  const platforms = deferredSupportedPlatforms();
  const selected = platforms.includes(platform) ? platform : platforms[0];
  const row = document.createElement("div");
  row.className = "deferred-target-row";
  row.innerHTML = `
    <label class="field">平台<select class="deferred-target-platform">${platforms.map((item) => `<option value="${escapeHtml(item)}" ${item === selected ? "selected" : ""}>${escapeHtml(platformLabel(item))}</option>`).join("")}</select></label>
    <label class="field">账号<small class="deferred-target-hint"></small><span class="deferred-account-slot"></span></label>
    <label class="field">可见性<select class="deferred-target-visibility"><option value="private" ${visibility === "public" ? "" : "selected"}>仅自己可见</option><option value="public" ${visibility === "public" ? "selected" : ""}>公开可见</option></select></label>
    <button class="icon-button remove-target" type="button" aria-label="删除发布目标">×</button>`;
  const platformSelect = row.querySelector(".deferred-target-platform");
  const visibilitySelect = row.querySelector(".deferred-target-visibility");
  const hint = row.querySelector(".deferred-target-hint");
  const syncHint = () => {
    hint.textContent = visibilitySelect.value === "public" ? "公开可见" : (DEFERRED_PLATFORM_HINTS[platformSelect.value] || "");
  };
  platformSelect.addEventListener("change", () => {
    renderDeferredAccountControl(row, platformSelect.value);
    syncHint();
    renderDeferredTargetSummary();
  });
  visibilitySelect.addEventListener("change", () => { syncHint(); renderDeferredTargetSummary(); });
  row.querySelector(".remove-target").addEventListener("click", () => { row.remove(); renderDeferredTargetSummary(); });
  renderDeferredAccountControl(row, selected, account);
  syncHint();
  elements["job-publish-targets"].appendChild(row);
  renderDeferredTargetSummary();
}

function updateDeferredPublishConfirmation() {
  elements["submit-job-publish"].disabled = elements["job-publish-confirm-input"].value.trim() !== "确认发布";
}

function renderPublishTargetSummary(container, targets) {
  if (!targets.length) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  const isPublicTarget = (target) => target.visibility === "public";
  const items = targets.map((target) => {
    const label = publicationVisibilityLabel(target.visibility) || "仅自己可见";
    const account = target.account ? `（${escapeHtml(target.account)}）` : "";
    return `<li><span>${escapeHtml(platformLabel(target.platform))}${account}</span><strong${isPublicTarget(target) ? ' class="visibility-public"' : ""}>${escapeHtml(label)}</strong></li>`;
  }).join("");
  const warning = targets.some(isPublicTarget)
    ? `<p class="visibility-public-note">包含“公开可见”目标：内容将对所有人公开且不可撤回，请确认无误。</p>` : "";
  container.innerHTML = `<ul class="publish-target-summary-list">${items}</ul>${warning}`;
  container.hidden = false;
}

function renderDeferredTargetSummary() {
  const targets = [...elements["job-publish-targets"].querySelectorAll(".deferred-target-row")].map((row) => ({
    platform: row.querySelector(".deferred-target-platform").value,
    account: row.querySelector(".deferred-target-account")?.value.trim() || "",
    visibility: row.querySelector(".deferred-target-visibility").value,
  }));
  renderPublishTargetSummary(elements["job-publish-target-summary"], targets);
}

function collectDeferredPublication() {
  const publishTargets = [];
  elements["job-publish-targets"].querySelectorAll(".deferred-target-row").forEach((row) => {
    const platform = row.querySelector(".deferred-target-platform").value;
    const account = row.querySelector(".deferred-target-account").value.trim();
    const visibility = row.querySelector(".deferred-target-visibility").value;
    if (!account) throw new Error(`请填写${platformLabel(platform)}账号。`);
    if (publishTargets.some((target) => target.platform === platform)) throw new Error("同一平台只能添加一次。");
    publishTargets.push({ platform, account, visibility });
  });
  if (!publishTargets.length) throw new Error("请至少选择一个发布平台。");
  const title = elements["job-publish-title"].value.trim();
  if (!title) throw new Error("请填写发布标题。");
  const tags = elements["job-publish-tags"].value.split(/[,，\n]/).map((item) => item.trim().replace(/^#/, "")).filter(Boolean);
  return {
    publish_targets: publishTargets,
    title,
    description: elements["job-publish-description"].value.trim(),
    tags,
  };
}

async function submitDeferredPublication(event) {
  event.preventDefault();
  const context = state.jobPublishContext;
  if (!context || elements["job-publish-confirm-input"].value.trim() !== "确认发布") return;
  let body;
  try {
    body = collectDeferredPublication();
  } catch (error) {
    elements["job-publish-alert"].hidden = false;
    elements["job-publish-alert"].textContent = error.message;
    return;
  }
  const button = elements["submit-job-publish"];
  button.disabled = true;
  button.textContent = "正在提交…";
  elements["job-publish-alert"].hidden = true;
  try {
    let requiredConfirmation;
    try {
      await apiRequest(`/jobs/${context.taskId}/publish`, { method: "POST", body });
      throw new Error("服务端未要求发布确认，已停止操作。");
    } catch (error) {
      const detail = error.payload?.detail;
      if (error instanceof ApiError && error.status === 409 && detail?.code === "publish_confirmation_required") {
        requiredConfirmation = detail.required_confirmation;
      } else {
        throw error;
      }
    }
    await apiRequest(`/jobs/${context.taskId}/publish`, {
      method: "POST",
      headers: { "X-AI-Pipeline-Publish-Confirmation": requiredConfirmation },
      body,
    });
    closeJobPublishDialog();
    state.selectedHasActivePublication = true;
    showToast("发布操作已进入队列", "success");
    await Promise.all([loadJobs(), loadTaskDetail(context.taskId, false)]);
    schedulePolling();
  } catch (error) {
    elements["job-publish-alert"].hidden = false;
    elements["job-publish-alert"].textContent = error.message;
  } finally {
    button.disabled = elements["job-publish-confirm-input"].value.trim() !== "确认发布";
    button.textContent = "确认并发布";
  }
}

function statusBadge(status) {
  return `<span class="badge ${escapeHtml(status || "neutral")}">${escapeHtml(STATUS_LABELS[status] || status || "未知")}</span>`;
}

function publicationStatusBadge(summary) {
  const stateName = summary?.state || "not_applicable";
  const classNames = {
    waiting_generation: "queued",
    not_published: "neutral",
    publishing: "running",
    published: "succeeded",
    partial: "partial",
    failed: "failed",
    not_applicable: "neutral",
  };
  return `<span class="badge publication-status ${escapeHtml(classNames[stateName] || "neutral")}" title="${escapeHtml(summary?.message || "无独立发布记录")}">${escapeHtml(summary?.label || "不适用")}</span>`;
}

function metaItem(label, value) {
  return `<div class="meta-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value ?? "—"))}</strong></div>`;
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" }).format(date);
}

function formatRelativeTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  if (Number.isNaN(seconds)) return value;
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}`;
}

function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  elements["toast-region"].appendChild(toast);
  setTimeout(() => toast.remove(), 4200);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
