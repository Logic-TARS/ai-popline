"use strict";

window.TaskScheduler = (() => {
  const scheduler = {
    request: null,
    toast: null,
    getPipelines: null,
    getPublishEnabled: null,
    editingId: null,
    schedules: [],
    cacheReport: null,
    loading: false,
    initialized: false,
  };

  const byId = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function initialize(dependencies) {
    if (scheduler.initialized) return;
    scheduler.initialized = true;
    scheduler.request = dependencies.request;
    scheduler.toast = dependencies.toast;
    scheduler.getPipelines = dependencies.getPipelines || (() => []);
    scheduler.getPublishEnabled = dependencies.getPublishEnabled || (() => false);
    byId("create-schedule-button").addEventListener("click", () => openDialog());
    byId("schedule-form").addEventListener("submit", submitSchedule);
    byId("schedule-frequency").addEventListener("change", toggleWeekdays);
    document.querySelectorAll("#schedule-dialog .close-dialog").forEach((button) => {
      button.addEventListener("click", () => byId("schedule-dialog").close());
    });
    byId("schedules-body").addEventListener("click", handleRowAction);
    byId("open-settings-center").addEventListener("click", openPublishPolicyDialog);
    byId("publish-policy-form").addEventListener("submit", submitPublishPolicy);
    document.querySelectorAll("#publish-policy-dialog .close-dialog").forEach((button) => {
      button.addEventListener("click", () => byId("publish-policy-dialog").close());
    });
    byId("settings-tabs").addEventListener("click", handleSettingsTabClick);
    byId("settings-tabs").addEventListener("keydown", handleSettingsTabKeydown);
    byId("publish-policy-content-nav").addEventListener("click", handlePolicyContentNavClick);
    byId("publish-policy-rows").addEventListener("click", handlePolicyRowsClick);
    byId("publish-policy-rows").addEventListener("change", handlePolicyRowsChange);
    byId("publish-policy-rows").addEventListener("input", updatePublishPolicySummary);
    byId("publish-policy-content-type").addEventListener("change", handlePolicyContentChange);
    byId("pipeline-defaults-enabled").addEventListener("change", updatePipelineDefaultsSummary);
    byId("pipeline-defaults-rows").addEventListener("input", handleDefaultsInput);
    byId("refresh-account-status").addEventListener("click", refreshAccountStatus);
    byId("scan-cache-button").addEventListener("click", scanCacheFiles);
    byId("clean-cache-button").addEventListener("click", cleanCacheFiles);
  }

  function closeDialog() {
    const dialog = byId("schedule-dialog");
    if (dialog?.open) dialog.close();
  }

  async function load() {
    if (!scheduler.request || scheduler.loading) return;
    scheduler.loading = true;
    try {
      const response = await scheduler.request("/schedules");
      scheduler.schedules = response.schedules || [];
      render();
      byId("schedule-updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    } catch (error) {
      if (error.status !== 401) scheduler.toast(error.message, "error");
    } finally {
      scheduler.loading = false;
    }
  }

  function pipelineLabel(contentType) {
    const match = scheduler.getPipelines().find((pipeline) => pipeline.content_type === contentType);
    return match ? match.label : (contentType || "自动路由");
  }

  function frequencyText(schedule) {
    if (schedule.frequency === "weekly") {
      const days = (schedule.weekday_labels || []).join("、") || "—";
      return `每周 ${days} ${schedule.time_of_day}`;
    }
    return `每天 ${schedule.time_of_day}`;
  }

  function formatTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function render() {
    const body = byId("schedules-body");
    byId("schedules-empty").hidden = scheduler.schedules.length > 0;
    body.innerHTML = scheduler.schedules.map((schedule) => `
      <tr data-schedule-id="${escapeHtml(schedule.schedule_id)}">
        <td><strong>${escapeHtml(schedule.name)}</strong>${schedule.publish ? ' <span class="badge partial">发布</span>' : ""}<br><small class="muted">${escapeHtml(schedule.description)}</small></td>
        <td>${escapeHtml(pipelineLabel(schedule.content_type))}</td>
        <td>${escapeHtml(frequencyText(schedule))}</td>
        <td>${schedule.enabled ? escapeHtml(formatTime(schedule.next_run_at)) : "已停用"}</td>
        <td>${escapeHtml(formatTime(schedule.last_run_at))}${schedule.last_error ? `<br><small class="muted">${escapeHtml(schedule.last_error)}</small>` : ""}</td>
        <td><span class="badge ${schedule.enabled ? "succeeded" : "neutral"}">${schedule.enabled ? "已启用" : "已停用"}</span></td>
        <td class="schedule-actions-cell"><span class="schedule-actions">
          <button class="button ghost compact" type="button" data-action="run">立即运行</button>
          <button class="button ghost compact" type="button" data-action="edit">编辑</button>
          <button class="button ghost compact" type="button" data-action="toggle">${schedule.enabled ? "停用" : "启用"}</button>
          <button class="button danger compact" type="button" data-action="delete">删除</button>
        </span></td>
      </tr>`).join("");
  }

  async function handleRowAction(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const row = button.closest("[data-schedule-id]");
    const scheduleId = row?.dataset.scheduleId;
    const schedule = scheduler.schedules.find((item) => item.schedule_id === scheduleId);
    if (!schedule) return;
    button.disabled = true;
    try {
      if (button.dataset.action === "toggle") {
        await scheduler.request(`/schedules/${scheduleId}`, { method: "PATCH", body: { enabled: !schedule.enabled } });
        scheduler.toast(schedule.enabled ? "定时任务已停用" : "定时任务已启用", "success");
      } else if (button.dataset.action === "edit") {
        openDialog(schedule);
        return;
      } else if (button.dataset.action === "delete") {
        if (!window.confirm(`确定删除定时任务「${schedule.name}」吗？`)) return;
        await scheduler.request(`/schedules/${scheduleId}`, { method: "DELETE" });
        scheduler.toast("定时任务已删除", "success");
      } else if (button.dataset.action === "run") {
        const result = await scheduler.request(`/schedules/${scheduleId}/run`, { method: "POST" });
        scheduler.toast(`已创建任务 ${result.task_id.slice(0, 8)}，可在任务中心查看进度`, "success");
      }
      await load();
    } catch (error) {
      scheduler.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function toggleWeekdays() {
    byId("schedule-weekdays-wrap").hidden = byId("schedule-frequency").value !== "weekly";
  }

  function openDialog(schedule = null) {
    scheduler.editingId = schedule?.schedule_id || null;
    const select = byId("schedule-pipeline");
    select.innerHTML = scheduler.getPipelines().map((pipeline) =>
      `<option value="${escapeHtml(pipeline.content_type)}">${escapeHtml(pipeline.label)}</option>`
    ).join("");
    if (!select.innerHTML) select.innerHTML = `<option value="">自动路由</option>`;
    byId("schedule-form").reset();
    byId("schedule-time").value = schedule?.time_of_day || "08:00";
    byId("schedule-dialog-title").textContent = schedule ? "编辑定时任务" : "创建定时任务";
    byId("schedule-dialog").querySelector(".eyebrow").textContent = schedule ? "EDIT SCHEDULE" : "NEW SCHEDULE";
    byId("submit-schedule-button").textContent = schedule ? "保存修改" : "保存定时任务";
    if (schedule) {
      byId("schedule-name").value = schedule.name || "";
      byId("schedule-frequency").value = schedule.frequency || "daily";
      select.value = schedule.content_type || "";
      byId("schedule-description").value = schedule.description || "";
      byId("schedule-topic").value = schedule.topic || "";
      const params = schedule.params || {};
      byId("schedule-params").value = Object.keys(params).length ? JSON.stringify(params, null, 2) : "";
      byId("schedule-weekdays").querySelectorAll("input").forEach((input) => {
        input.checked = (schedule.weekdays || []).includes(Number(input.value));
      });
    }
    const publishToggle = byId("schedule-publish-toggle");
    const publishEnabled = scheduler.getPublishEnabled();
    publishToggle.disabled = !publishEnabled;
    publishToggle.checked = publishEnabled && Boolean(schedule?.publish);
    byId("schedule-publish-help").textContent = publishEnabled
      ? "开启后每次运行都会在生成并校验后尝试发布到配置的账号。"
      : "服务端 Web 发布当前安全关闭，定时任务只能生成内容。";
    toggleWeekdays();
    byId("schedule-dialog").showModal();
  }

  function selectedWeekdays() {
    return [...byId("schedule-weekdays").querySelectorAll("input:checked")].map((input) => Number(input.value));
  }

  async function submitSchedule(event) {
    event.preventDefault();
    const frequency = byId("schedule-frequency").value;
    const weekdays = frequency === "weekly" ? selectedWeekdays() : [];
    if (frequency === "weekly" && !weekdays.length) {
      scheduler.toast("每周任务请至少选择一个星期", "error");
      return;
    }
    let params = {};
    const rawParams = byId("schedule-params").value.trim();
    if (rawParams) {
      try {
        params = JSON.parse(rawParams);
      } catch {
        scheduler.toast("流水线参数不是有效的 JSON", "error");
        return;
      }
      if (!params || typeof params !== "object" || Array.isArray(params)) {
        scheduler.toast("流水线参数必须是 JSON 对象", "error");
        return;
      }
    }
    const contentType = byId("schedule-pipeline").value;
    const body = {
      name: byId("schedule-name").value.trim(),
      frequency,
      time_of_day: byId("schedule-time").value,
      weekdays,
      task: {
        description: byId("schedule-description").value.trim(),
        topic: byId("schedule-topic").value.trim() || null,
        content_type: contentType || null,
        publish: byId("schedule-publish-toggle").checked,
        params,
      },
    };
    const submit = byId("submit-schedule-button");
    submit.disabled = true;
    try {
      if (scheduler.editingId) {
        await scheduler.request(`/schedules/${scheduler.editingId}`, { method: "PATCH", body });
        scheduler.toast("定时任务已更新", "success");
      } else {
        await scheduler.request("/schedules", { method: "POST", body });
        scheduler.toast("定时任务已创建", "success");
      }
      scheduler.editingId = null;
      byId("schedule-dialog").close();
      await load();
    } catch (error) {
      scheduler.toast(error.message, "error");
    } finally {
      submit.disabled = false;
    }
  }

  const PUBLISH_CONTENT_LABELS = {
    finance: "金融简报",
    ai_briefing: "AI 简报",
    script_video: "口播视频",
    grouped_anime: "分组动漫",
    ai_art: "AI 绘画",
  };
  const PIPELINE_CONTENT_LABELS = {
    ...PUBLISH_CONTENT_LABELS,
    japanese: "日语视觉化",
    xhs_image_note: "小红书图文",
    anime: "动漫视频",
  };
  const POLICY_PLATFORM_LABELS = { douyin: "抖音", kuaishou: "快手", tencent: "视频号", bilibili: "Bilibili", xiaohongshu: "小红书" };
  const CACHE_CATEGORY_LABELS = { "python-cache": "Python 字节码", "tool-cache": "工具缓存", coverage: "覆盖率数据", "build-output": "构建产物", extra: "指定目录" };

  function setSettingsLoading(loading) {
    byId("settings-loading").hidden = !loading;
    byId("submit-publish-policy").disabled = loading;
    byId("refresh-account-status").disabled = loading;
  }

  function activateSettingsTab(name, focus = false) {
    document.querySelectorAll("[data-settings-tab]").forEach((tab) => {
      const selected = tab.dataset.settingsTab === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    });
    document.querySelectorAll("[data-settings-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.settingsPanel !== name;
    });
  }

  function handleSettingsTabClick(event) {
    const tab = event.target.closest("[data-settings-tab]");
    if (tab) activateSettingsTab(tab.dataset.settingsTab);
  }

  function handleSettingsTabKeydown(event) {
    const tab = event.target.closest("[data-settings-tab]");
    if (!tab) return;
    const tabs = [...byId("settings-tabs").querySelectorAll("[data-settings-tab]")];
    const current = tabs.indexOf(tab);
    let next = current;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (current + 1) % tabs.length;
    else if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (current - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    activateSettingsTab(tabs[next].dataset.settingsTab, true);
  }

  async function openPublishPolicyDialog() {
    const dialog = byId("publish-policy-dialog");
    activateSettingsTab("publish");
    byId("settings-save-status").textContent = "更改仅在保存后生效";
    byId("settings-save-status").classList.remove("error");
    if (!dialog.open) dialog.showModal();
    setSettingsLoading(true);
    try {
      const [policyData, defaultsData, accountsData] = await Promise.all([
        scheduler.request("/publish-policy"),
        scheduler.request("/pipeline-defaults"),
        scheduler.request("/account-status"),
      ]);
      renderPolicyRows(policyData.policy || {}, policyData.supported_platforms || []);
      renderPipelineDefaultsRows(defaultsData);
      renderAccountStatusRows(accountsData);
      renderAccountDatalists(accountsData.accounts);
    } catch (error) {
      scheduler.toast(error.message, "error");
      byId("settings-save-status").textContent = "设置加载失败，请关闭后重试";
      byId("settings-save-status").classList.add("error");
    } finally {
      setSettingsLoading(false);
    }
  }

  function validateDefaultsRow(row) {
    const input = row.querySelector("[data-defaults-params]");
    const status = row.querySelector("[data-defaults-status]");
    const message = row.querySelector("[data-defaults-validation]");
    const text = input.value.trim();
    let params = null;
    let error = "";
    if (text) {
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) params = parsed;
        else error = "必须输入 JSON 对象";
      } catch {
        error = "JSON 格式有误";
      }
    }
    input.setAttribute("aria-invalid", String(Boolean(error)));
    status.className = `defaults-card-status${error ? " invalid" : (params ? " configured" : "")}`;
    status.textContent = error ? "格式错误" : (params ? `${Object.keys(params).length} 个参数` : "未配置");
    message.textContent = error || (params ? "格式有效，将在保存后应用。" : "留空时不覆盖该流水线参数。");
    message.classList.toggle("error", Boolean(error));
    return { valid: !error, params };
  }

  function updatePipelineDefaultsSummary() {
    let configured = 0;
    let count = 0;
    let invalid = 0;
    document.querySelectorAll("[data-defaults-row]").forEach((row) => {
      const result = validateDefaultsRow(row);
      if (!result.valid) invalid += 1;
      if (result.params) {
        configured += 1;
        count += Object.keys(result.params).length;
      }
    });
    const state = byId("pipeline-defaults-enabled").checked ? "已启用" : "已停用";
    byId("pipeline-defaults-summary").innerHTML = invalid
      ? `<strong>${invalid}</strong> 项格式错误`
      : `${state} · <strong>${configured}</strong> 项配置 · ${count} 个参数`;
  }

  function handleDefaultsInput(event) {
    const row = event.target.closest("[data-defaults-row]");
    if (!row) return;
    validateDefaultsRow(row);
    updatePipelineDefaultsSummary();
  }

  function renderPipelineDefaultsRows(data) {
    byId("pipeline-defaults-enabled").checked = Boolean(data?.enabled);
    const pipelines = data?.pipelines || {};
    byId("pipeline-defaults-rows").innerHTML = Object.entries(PIPELINE_CONTENT_LABELS).map(([contentType, label]) => {
      const params = pipelines[contentType]?.params || {};
      const text = Object.keys(params).length ? JSON.stringify(params, null, 2) : "";
      const inputId = `pipeline-defaults-${contentType}`;
      return `<details class="defaults-card" data-defaults-row="${escapeHtml(contentType)}">
        <summary><span class="defaults-card-title"><strong>${escapeHtml(label)}</strong><small>${escapeHtml(contentType)}</small></span><span class="defaults-card-status" data-defaults-status>未配置</span></summary>
        <div class="defaults-card-body"><label class="sr-only" for="${escapeHtml(inputId)}">${escapeHtml(label)}默认参数 JSON</label><textarea id="${escapeHtml(inputId)}" rows="5" spellcheck="false" data-defaults-params placeholder="{}" aria-describedby="${escapeHtml(inputId)}-validation">${escapeHtml(text)}</textarea><p id="${escapeHtml(inputId)}-validation" class="defaults-validation" data-defaults-validation></p></div>
      </details>`;
    }).join("");
    updatePipelineDefaultsSummary();
  }

  function accountState(item, source) {
    const status = String(item.status || "").trim().toLowerCase();
    const label = String(item.status_label || "").trim();
    if (source !== "bridge") {
      return item.has_cookie
        ? { tone: "warning", label: "待验证", detail: "检测到本机 cookie，账号中心离线，无法确认登录是否有效。" }
        : { tone: "unavailable", label: "不可用", detail: "未检测到可用 cookie。" };
    }
    const available = ["valid", "active", "ready", "online", "authenticated", "logged_in", "ok"].includes(status);
    const unavailable = ["login_required", "expired", "invalid", "offline", "error", "unavailable", "missing"].includes(status);
    if (available) return { tone: "available", label: label || "有效", detail: "SAU 账号中心已验证当前登录状态。" };
    if (unavailable) return { tone: "unavailable", label: label || "需重新登录", detail: "SAU 账号中心报告此账号当前不可用于发布。" };
    return { tone: "warning", label: label || "状态未知", detail: "账号中心已连接，但未返回可识别的登录状态。" };
  }

  function renderAccountStatusRows(data) {
    const accounts = data?.accounts || [];
    const source = data?.source === "bridge" ? "bridge" : "cookies";
    const sourceLabel = source === "bridge" ? "SAU 账号中心 · 实时验证" : "本机 cookie 文件 · 离线推断";
    const states = accounts.map((item) => accountState(item, source));
    const availableCount = states.filter((state) => state.tone === "available").length;
    byId("account-status-source").innerHTML = `<span class="account-source-badge">${escapeHtml(sourceLabel)}</span> · ${accounts.length} 个账号${source === "bridge" ? ` · ${availableCount} 个可用` : ""}`;
    const container = byId("account-status-rows");
    if (!accounts.length) {
      container.innerHTML = `<p class="muted">未发现任何平台账号。请检查 SAU 账号中心连接或本机 cookie 文件。</p>`;
      return;
    }
    container.innerHTML = accounts.map((item, index) => {
      const platform = POLICY_PLATFORM_LABELS[item.platform] || item.platform;
      const state = states[index];
      const modified = item.cookie_modified ? new Date(item.cookie_modified).toLocaleString("zh-CN", { hour12: false }) : "无本机 cookie";
      return `<article class="account-card"><header><div><h4>${escapeHtml(platform)}</h4><p>${escapeHtml(item.account)}</p></div><span class="account-state ${state.tone}">${escapeHtml(state.label)}</span></header><p>${escapeHtml(state.detail)}</p><div class="account-card-meta"><span>来源：${escapeHtml(source === "bridge" ? "账号中心" : "本机文件")}</span><span>Cookie：${escapeHtml(modified)}</span></div></article>`;
    }).join("");
  }

  async function refreshAccountStatus() {
    const button = byId("refresh-account-status");
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "刷新中…";
    byId("account-status-rows").setAttribute("aria-busy", "true");
    try {
      const data = await scheduler.request("/account-status");
      renderAccountStatusRows(data);
      renderAccountDatalists(data.accounts);
      scheduler.toast("账号状态已刷新", "success");
    } catch (error) {
      scheduler.toast(error.message, "error");
    } finally {
      byId("account-status-rows").removeAttribute("aria-busy");
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  function formatBytes(size) {
    const value = Number(size) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(2)} MB`;
  }

  function renderCacheCleanup(data) {
    scheduler.cacheReport = data || null;
    const items = data?.items || [];
    const failures = data?.failures || [];
    const total = Number(data?.total) || 0;
    const size = formatBytes(data?.total_size);
    byId("cache-cleanup-summary").innerHTML = failures.length
      ? `清理完成 · <strong>${failures.length}</strong> 项失败`
      : (data?.applied
        ? `已清理 <strong>${total}</strong> 项 · 释放 ${escapeHtml(size)}`
        : `发现 <strong>${total}</strong> 项 · ${escapeHtml(size)}`);
    byId("clean-cache-button").disabled = Boolean(data?.applied) || items.length === 0;
    if (!items.length) {
      byId("cache-cleanup-rows").innerHTML = `<p class="muted">${data?.applied ? "缓存已清理干净。" : "没有发现可清理的缓存。"}</p>`;
      return;
    }
    const rows = items.map((item) => `<div class="cache-cleanup-row"><span class="cache-cleanup-category">${escapeHtml(CACHE_CATEGORY_LABELS[item.category] || item.category)}</span><code>${escapeHtml(item.path)}${item.is_dir ? "/" : ""}</code><span class="cache-cleanup-size">${escapeHtml(formatBytes(item.size))}</span></div>`);
    const errors = failures.map((failure) => `<p class="cache-cleanup-failure">${escapeHtml(failure.path)}：${escapeHtml(failure.error)}</p>`);
    byId("cache-cleanup-rows").innerHTML = rows.concat(errors).join("");
  }

  async function scanCacheFiles() {
    const button = byId("scan-cache-button");
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "扫描中…";
    byId("clean-cache-button").disabled = true;
    try {
      renderCacheCleanup(await scheduler.request("/maintenance/cache-cleanup", { method: "POST", body: { apply: false } }));
    } catch (error) {
      scheduler.toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  async function cleanCacheFiles() {
    const pending = scheduler.cacheReport?.items?.length || 0;
    if (!pending) {
      scheduler.toast("请先扫描缓存再清理", "warning");
      return;
    }
    const confirmed = window.confirm(`将删除 ${pending} 项缓存（约 ${formatBytes(scheduler.cacheReport.total_size)}），此操作不可撤销。确认继续？`);
    if (!confirmed) return;
    const button = byId("clean-cache-button");
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "清理中…";
    try {
      const data = await scheduler.request("/maintenance/cache-cleanup", { method: "POST", body: { apply: true } });
      renderCacheCleanup(data);
      const failures = data?.failures?.length || 0;
      if (failures) scheduler.toast(`已清理 ${(Number(data?.total) || 0) - failures} 项，${failures} 项失败`, "warning");
      else scheduler.toast(`已清理 ${Number(data?.total) || 0} 项缓存，释放 ${formatBytes(data?.total_size)}`, "success");
    } catch (error) {
      scheduler.toast(error.message, "error");
    } finally {
      button.textContent = originalText;
      if (!scheduler.cacheReport?.applied) button.disabled = false;
    }
  }

  function updatePolicyContentNav() {
    const selected = byId("publish-policy-content-type").value;
    byId("publish-policy-content-nav").innerHTML = Object.entries(PUBLISH_CONTENT_LABELS).map(([contentType, label]) => {
      const entry = scheduler.publishPolicy[contentType] || { enabled: false, targets: [] };
      return `<button class="policy-content-button" type="button" data-policy-content="${escapeHtml(contentType)}" aria-pressed="${contentType === selected}"><strong>${escapeHtml(label)}</strong><small>${entry.enabled ? "已启用" : "未启用"} · ${entry.targets.length} 个目标</small></button>`;
    }).join("");
  }

  function updatePublishPolicySummary() {
    saveVisiblePolicy(false);
    const entries = Object.values(scheduler.publishPolicy || {});
    const enabled = entries.filter((entry) => entry.enabled).length;
    const targets = entries.reduce((count, entry) => count + entry.targets.length, 0);
    byId("publish-policy-summary").innerHTML = `<strong>${enabled}</strong> 种已启用 · ${targets} 个目标`;
    updatePolicyContentNav();
  }

  function renderPolicyRows(policy, platforms) {
    scheduler.policyPlatforms = platforms;
    scheduler.publishPolicy = {};
    Object.keys(PUBLISH_CONTENT_LABELS).forEach((contentType) => {
      const entry = policy[contentType] || { enabled: false, targets: [] };
      scheduler.publishPolicy[contentType] = {
        enabled: Boolean(entry.enabled),
        targets: (entry.targets || []).map((target) => ({ ...target })),
      };
    });
    const select = byId("publish-policy-content-type");
    select.innerHTML = Object.entries(PUBLISH_CONTENT_LABELS).map(([contentType, label]) =>
      `<option value="${escapeHtml(contentType)}">${escapeHtml(label)}</option>`
    ).join("");
    select.dataset.previous = select.value;
    renderSelectedPolicy();
    updatePublishPolicySummary();
  }

  function renderPolicyTargetEmpty() {
    const list = byId("publish-policy-rows").querySelector("[data-policy-targets]");
    if (!list) return;
    const hasTargets = Boolean(list.querySelector("[data-policy-target]"));
    list.querySelector(".policy-target-empty")?.remove();
    if (!hasTargets) list.insertAdjacentHTML("beforeend", `<p class="policy-target-empty">尚未添加发布目标。启用策略前请添加并确认账号。</p>`);
  }

  function renderSelectedPolicy() {
    const contentType = byId("publish-policy-content-type").value || Object.keys(PUBLISH_CONTENT_LABELS)[0];
    const label = PUBLISH_CONTENT_LABELS[contentType] || contentType;
    const entry = scheduler.publishPolicy[contentType] || { enabled: false, targets: [] };
    const targets = entry.targets.map((target) => policyTargetRowHtml(target)).join("");
    byId("publish-policy-rows").innerHTML = `<section class="settings-content-card" data-policy-row="${escapeHtml(contentType)}">
      <label class="switch-row"><span><strong>${escapeHtml(label)}默认发布</strong><small>生成完成并通过校验后，默认发布到下列账号。</small></span><input type="checkbox" role="switch" data-policy-enabled ${entry.enabled ? "checked" : ""}></label>
      <div class="policy-targets" data-policy-targets>${targets}</div>
      <button type="button" class="button ghost compact" data-add-target>＋ 添加发布目标</button>
    </section>`;
    renderPolicyTargetEmpty();
    updatePolicyContentNav();
  }

  function saveVisiblePolicy(requireAccounts = true) {
    const row = byId("publish-policy-rows").querySelector("[data-policy-row]");
    if (!row) return true;
    const contentType = row.dataset.policyRow;
    const targets = [];
    let firstInvalid = null;
    row.querySelectorAll("[data-policy-target]").forEach((targetRow) => {
      const platform = targetRow.querySelector("[data-target-platform]").value;
      const accountInput = targetRow.querySelector("[data-target-account]");
      const account = accountInput.value.trim();
      const visibility = targetRow.querySelector("[data-target-visibility]").value;
      accountInput.setAttribute("aria-invalid", String(requireAccounts && !account));
      if (!account && !firstInvalid) firstInvalid = accountInput;
      targets.push({ platform, account, visibility });
    });
    scheduler.publishPolicy[contentType] = {
      enabled: row.querySelector("[data-policy-enabled]").checked,
      targets,
    };
    if (requireAccounts && firstInvalid) {
      firstInvalid.focus();
      return false;
    }
    return true;
  }

  function handlePolicyContentNavClick(event) {
    const button = event.target.closest("[data-policy-content]");
    if (!button) return;
    const select = byId("publish-policy-content-type");
    if (button.dataset.policyContent === select.value) return;
    select.value = button.dataset.policyContent;
    handlePolicyContentChange({ target: select });
  }

  function handlePolicyContentChange(event) {
    const previous = event.target.dataset.previous;
    if (previous && !saveVisiblePolicy()) {
      scheduler.toast("请先填写当前发布目标的账号", "error");
      event.target.value = previous;
      updatePolicyContentNav();
      return;
    }
    event.target.dataset.previous = event.target.value;
    renderSelectedPolicy();
    updatePublishPolicySummary();
  }

  function policyTargetRowHtml(target) {
    const platforms = scheduler.policyPlatforms || [];
    const platform = target?.platform || platforms[0] || "douyin";
    const visibility = target?.visibility || "private";
    const options = platforms.map((item) =>
      `<option value="${escapeHtml(item)}" ${item === platform ? "selected" : ""}>${escapeHtml(POLICY_PLATFORM_LABELS[item] || item)}</option>`
    ).join("");
    return `<div class="policy-target" data-policy-target>
      <label class="field policy-platform-field">发布平台<select data-target-platform>${options}</select></label>
      <label class="field policy-account-field">目标账号<input data-target-account list="policy-accounts-${escapeHtml(platform)}" placeholder="选择或输入账号" value="${escapeHtml(target?.account || "")}"></label>
      <label class="field policy-visibility-field">可见范围<select data-target-visibility class="${visibility === "public" ? "visibility-public" : ""}"><option value="private" ${visibility === "private" ? "selected" : ""}>仅自己可见</option><option value="public" ${visibility === "public" ? "selected" : ""}>公开可见</option></select></label>
      <button type="button" class="icon-button" data-remove-target aria-label="删除发布目标">×</button>
    </div>`;
  }

  function renderAccountDatalists(accounts) {
    const byPlatform = {};
    (accounts || []).forEach((item) => {
      if (!item.platform || !item.account) return;
      (byPlatform[item.platform] ||= new Set()).add(item.account);
    });
    byId("policy-account-datalists").innerHTML = Object.entries(byPlatform).map(([platform, names]) =>
      `<datalist id="policy-accounts-${escapeHtml(platform)}">${[...names].map((name) => `<option value="${escapeHtml(name)}"></option>`).join("")}</datalist>`
    ).join("");
  }

  function handlePolicyRowsClick(event) {
    const addButton = event.target.closest("[data-add-target]");
    if (addButton) {
      const list = addButton.closest("[data-policy-row]").querySelector("[data-policy-targets]");
      list.querySelector(".policy-target-empty")?.remove();
      list.insertAdjacentHTML("beforeend", policyTargetRowHtml(null));
      list.querySelector("[data-policy-target]:last-child [data-target-platform]")?.focus();
      updatePublishPolicySummary();
      return;
    }
    const removeButton = event.target.closest("[data-remove-target]");
    if (removeButton) {
      removeButton.closest("[data-policy-target]").remove();
      renderPolicyTargetEmpty();
      updatePublishPolicySummary();
    }
  }

  function handlePolicyRowsChange(event) {
    if (event.target.matches("[data-target-platform]")) {
      const input = event.target.closest("[data-policy-target]").querySelector("[data-target-account]");
      input.setAttribute("list", `policy-accounts-${event.target.value}`);
      input.value = "";
    }
    if (event.target.matches("[data-target-visibility]")) {
      event.target.classList.toggle("visibility-public", event.target.value === "public");
    }
    updatePublishPolicySummary();
  }

  async function submitPublishPolicy(event) {
    event.preventDefault();
    if (!saveVisiblePolicy()) {
      const contentType = byId("publish-policy-content-type").value;
      activateSettingsTab("publish");
      scheduler.toast(`「${PUBLISH_CONTENT_LABELS[contentType] || contentType}」存在未填写账号的发布目标`, "error");
      return;
    }
    const policy = scheduler.publishPolicy;
    const pipelineParams = {};
    let invalidRow = null;
    document.querySelectorAll("[data-defaults-row]").forEach((row) => {
      const result = validateDefaultsRow(row);
      if (!result.valid && !invalidRow) invalidRow = row;
      if (result.params) pipelineParams[row.dataset.defaultsRow] = { params: result.params };
    });
    if (invalidRow) {
      activateSettingsTab("defaults");
      invalidRow.open = true;
      const input = invalidRow.querySelector("[data-defaults-params]");
      input.focus();
      input.scrollIntoView({ block: "center" });
      scheduler.toast(`流水线默认参数不是有效的 JSON 对象：${invalidRow.dataset.defaultsRow}`, "error");
      return;
    }
    const submit = byId("submit-publish-policy");
    const originalText = submit.textContent;
    submit.disabled = true;
    submit.textContent = "保存中…";
    byId("settings-save-status").textContent = "正在保存默认参数和发布策略…";
    byId("settings-save-status").classList.remove("error");
    try {
      await scheduler.request("/pipeline-defaults", {
        method: "PUT",
        body: { enabled: byId("pipeline-defaults-enabled").checked, pipelines: pipelineParams },
      });
      await scheduler.request("/publish-policy", { method: "PUT", body: { policy } });
      scheduler.toast("设置已保存", "success");
      byId("settings-save-status").textContent = "设置已保存";
      byId("publish-policy-dialog").close();
    } catch (error) {
      scheduler.toast(error.message, "error");
      byId("settings-save-status").textContent = "保存失败，请检查后重试";
      byId("settings-save-status").classList.add("error");
    } finally {
      submit.disabled = false;
      submit.textContent = originalText;
    }
  }

  return { initialize, load, closeDialog };
})();
