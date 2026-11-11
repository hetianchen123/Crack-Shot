/* Crack-shot 后台前端：原生 JS，轮询刷新，无构建依赖。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = {
  async call(path, opts = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!resp.ok) {
      let msg = `${resp.status}`;
      try { msg = (await resp.json()).detail || msg; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    return resp.status === 204 ? null : resp.json();
  },
  get: (p) => api.call(p),
  post: (p, body) => api.call(p, { method: "POST", body: JSON.stringify(body || {}) }),
  patch: (p, body) => api.call(p, { method: "PATCH", body: JSON.stringify(body || {}) }),
  put: (p, body) => api.call(p, { method: "PUT", body: JSON.stringify(body || {}) }),
  del: (p) => api.call(p, { method: "DELETE" }),
};

const STATUS_LABEL = {
  draft: "草稿", ready: "校准待确认",
  scheduled: "已排程", prewarming: "预热中", grabbing: "抢票中", success: "成功",
  needs_human: "需人工", failed: "失败", timeout: "超时", cancelled: "已取消",
  sold_out: "缺货",
  conflict: "设备冲突", waiting_device: "等设备", error: "异常",
};
const OUTCOME_LABEL = {
  success: "下单成功", needs_human: "需人工", timeout: "窗口超时", off_page: "离开页面",
  sold_out: "缺货",
  cancelled: "已取消", error: "异常", calibrated: "校准完成",
  guard_verified: "守护演练通过", "": "运行中",
};

let devicesCache = [];
let accountsCache = [];
let tasksCache = new Map();
let tasksRequest = 0;

/* ---------- 原生 alert/confirm 的页内替代（内嵌浏览器里原生对话框不可靠） ---------- */

let _confirmResolve = null;
function confirmDialog(message, title = "确认") {
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  const dlg = $("#confirm-dialog");
  dlg.showModal();
  return new Promise((resolve) => { _confirmResolve = resolve; });
}
document.querySelector("#confirm-ok").addEventListener("click", () => {
  $("#confirm-dialog").close();
  if (_confirmResolve) { _confirmResolve(true); _confirmResolve = null; }
});
document.querySelector("#confirm-cancel").addEventListener("click", () => {
  $("#confirm-dialog").close();
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
});
function alertDialog(message) {
  return confirmDialog(message, "提示");
}
const confirm = confirmDialog; // 后文沿用 confirm(...) 写法
const alert = alertDialog;

/* ---------- 基础 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function badge(kind, text) {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}
function countdown(ms) {
  if (ms <= 0) return "已到点";
  const s = Math.ceil(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
    m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}天${h}时`;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

let overviewClock;
function updateCountdowns() {
  if (!overviewClock) return;
  const now = overviewClock.serverMs + performance.now() - overviewClock.receivedAt;
  document.querySelectorAll("#ov-next [data-trigger-ms]").forEach((cell) => {
    cell.textContent = countdown(Number(cell.dataset.triggerMs) - now);
  });
}

/* ---------- 导航 ---------- */

document.querySelectorAll("#nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    refreshAll();
  });
});

/* ---------- 总览 ---------- */

async function renderOverview() {
  const data = await api.get("/api/overview");
  overviewClock = {serverMs: data.server_now_ms, receivedAt: performance.now()};
  devicesCache = data.devices;
  const tasks = data.tasks;
  const counts = { enabled: 0, scheduled: 0, running: 0, success: 0, attention: 0 };
  for (const t of tasks) {
    if (t.enabled) counts.enabled += 1;
    if (t.status === "scheduled") counts.scheduled += 1;
    if (t.status === "prewarming" || t.status === "grabbing") counts.running += 1;
    if (t.status === "success") counts.success += 1;
    if (["needs_human", "conflict", "error", "waiting_device"].includes(t.status)) counts.attention += 1;
  }
  $("#ov-cards").innerHTML = `
    <div class="card"><div class="num">${counts.enabled}</div><div class="label">启用任务</div></div>
    <div class="card"><div class="num">${counts.running}</div><div class="label">执行中</div></div>
    <div class="card"><div class="num">${counts.scheduled}</div><div class="label">待开票</div></div>
    <div class="card"><div class="num">${counts.success}</div><div class="label">已成功</div></div>
    <div class="card"><div class="num" style="color:${counts.attention ? "var(--warn)" : "inherit"}">${counts.attention}</div><div class="label">需关注</div></div>`;

  $("#ov-next").innerHTML = data.next_up.length ? `<table>
    <tr><th>任务</th><th>演出</th><th>设备/账号</th><th>开票时间</th><th>倒计时</th><th>状态</th></tr>
    ${data.next_up.map((t) => `<tr>
      <td>${esc(t.name)}</td><td>${esc(t.show_name)}</td>
      <td>${esc(t.device_serial || "未绑定")}${t.account_label ? ` / ${esc(t.account_label)}` : ""}</td>
      <td class="mono">${esc(t.on_sale_at)}</td>
      <td class="mono"${Number.isFinite(t.trigger_at_ms) ? ` data-trigger-ms="${t.trigger_at_ms}"` : ""}></td>
      <td>${badge(t.status, t.scheduled_dry_run && t.status === "scheduled"
        ? "定时演练已排程" : STATUS_LABEL[t.status] || t.status)}</td>
    </tr>`).join("")}</table>` : `<div class="empty">没有待开票任务</div>`;
  updateCountdowns();

  $("#ov-runs").innerHTML = data.runs.length ? `<table>
    <tr><th>#</th><th>任务</th><th>开始</th><th>结果</th><th>尝试</th><th></th></tr>
    ${data.runs.map((r) => `<tr>
      <td class="mono">${r.id}</td><td>${esc(r.task_name)}</td>
      <td class="mono">${esc(r.started_at)}</td>
      <td>${badge(r.outcome || "running", OUTCOME_LABEL[r.outcome] || r.outcome || "运行中")}
        ${r.dry_run ? badge("dry", "演练") : ""}</td>
      <td class="mono">${r.attempts}</td>
      <td><button class="small" onclick="showRun(${r.id})">详情</button></td>
    </tr>`).join("")}</table>` : `<div class="empty">还没有运行记录</div>`;

  $("#ov-devices").innerHTML = data.devices.length ? `<table>
    <tr><th>设备</th><th>绑定账号</th><th>在线</th><th>当前任务</th></tr>
    ${data.devices.map((d) => `<tr>
      <td class="mono">${esc(d.serial)}<span class="muted"> ${esc(d.model || "")}</span></td>
      <td>${esc(d.account_label || "未绑定")}</td>
      <td>${d.online ? "✅" : "❌"}</td>
      <td>${d.busy_task ? esc(d.busy_task) : '<span class="muted">空闲</span>'}</td>
    </tr>`).join("")}</table>` : `<div class="empty">没有注册设备，请到「设备」页注册</div>`;
}

/* ---------- 任务 ---------- */

function scheduleMode(task) {
  if (!task?.prewarm_at) return null;
  return task.scheduled_dry_run ? "rehearsal" : "formal";
}

function taskStatus(task, mode) {
  if (mode && !task.enabled) return badge("cancelled", "排程已暂停");
  if (!mode && task.status === "scheduled") return badge("failed", "排程时间无效");
  const status = badge(task.status, STATUS_LABEL[task.status] || task.status);
  if (!mode) return status;
  const scheduled = badge("scheduled", mode === "rehearsal" ? "定时演练已排程" : "已排程");
  return task.status === "scheduled" ? scheduled : `${scheduled}<br>${status}`;
}

function priceList(task) {
  return task.price_tiers.length === 1 ? `¥${task.price_tiers[0]}` : "待确认预约票档";
}

async function renderTasks() {
  const request = ++tasksRequest;
  const [tasks, devices] = await Promise.all([api.get("/api/tasks"), api.get("/api/devices")]);
  if (request !== tasksRequest) return;
  devicesCache = devices.devices;
  const deviceOptions = ['<option value="">未绑定</option>']
    .concat(devices.devices.map((d) =>
      `<option value="${d.id}">${esc(d.serial)} ${d.account_label ? `(${esc(d.account_label)})` : ""}</option>`))
    .join("");
  const sel = $("#task-device-select");
  const prev = sel.value;
  sel.innerHTML = deviceOptions;
  if (prev) sel.value = prev;
  renderTaskRows(tasks);
}

function renderTaskRows(tasks) {
  tasksCache = new Map(tasks.map((t) => [t.id, t]));
  $("#task-list").innerHTML = tasks.length ? `<table>
    <tr><th>任务</th><th>演出与日期</th><th>开票</th><th>预约票档</th><th>设备/账号</th><th>状态</th><th>操作</th></tr>
    ${tasks.map((t) => {
      const mode = scheduleMode(t);
      const check = t.calibration || {};
      return `<tr>
      <td>${esc(t.name)} ${t.enabled ? "" : badge("", "停用")}</td>
      <td>${esc(t.show_name)}${t.city ? ` · ${esc(t.city)}` : ""}
        <br><span class="muted">目标日期 ${esc(check.target_date || t.session || "未填写")}</span>
        ${check.date_check ? `<br><span class="muted">页面日期 ${esc(check.page_date || "未读到")}</span>
          <br><span class="muted">${esc(check.date_check)}</span>` : ""}</td>
      <td class="mono">${esc(t.on_sale_at || "未确认")}
        ${mode ? `<br><span class="muted">预热时间 ${esc(t.prewarm_at)}</span>` : ""}
        ${t.calibrated_at ? `<br><span class="muted">页面校准 ${esc(t.calibrated_at)}</span>` : ""}</td>
      <td class="mono">${esc(priceList(t))}
        ${check.price_check ? `<br><span class="muted">页面票价 ${esc(check.page_price || "未读到")}</span>
          <br><span class="muted">${esc(check.price_check)}</span>` : ""}
        ${check.reservation_error ? `<br><span class="muted">${esc(check.reservation_error)}</span>` : ""}</td>
      <td>${esc(t.device_serial || "未绑定")}${t.account_label ? `<br><span class="muted">${esc(t.account_label)}</span>` : ""}</td>
      <td>${taskStatus(t, mode)}${check.mismatch ? `<br>${badge("error", "核对有差异")}` : ""}</td>
      <td><div class="row-actions">
        ${!["prewarming", "grabbing"].includes(t.status) && t.enabled && t.device_id ?
          `<button class="small" onclick="calibrateTask(${t.id})">校准</button>
           ${t.calibrated_at && t.session && t.price_tiers.length === 1 && !check.mismatch ? `<button class="small${mode === "formal" ? "" : " primary"}" onclick="openSchedule(${t.id}, false)">${mode === "formal" ? "修改排程" : "确认开抢时间并排程"}</button>` : ""}
           ${mode !== "formal" ? `<button class="small" onclick="openSchedule(${t.id}, true)">${mode === "rehearsal" ? "修改演练" : "定时演练"}</button>` : ""}
           <button class="small" onclick="runTask(${t.id}, true)">立即演练</button>
           ${mode === "formal" && t.price_tiers.length === 1 ? `<button class="small" onclick="runTask(${t.id}, false)">立即执行</button>` : ""}` : ""}
        ${["prewarming", "grabbing"].includes(t.status) ?
          `<button class="small danger" onclick="cancelTask(${t.id})">停止</button>` : ""}
        <button class="small" onclick="openTaskForm(tasksCache.get(${t.id}))">编辑</button>
        <button class="small" onclick="showTaskEvents(${t.id})">日志</button>
        <button class="small" onclick="toggleTask(${t.id}, ${t.enabled ? 0 : 1})">${t.enabled ? "停用" : "启用"}</button>
        <button class="small danger" onclick="deleteTask(${t.id})">删除</button>
      </div></td>
    </tr>`;
    }).join("")}</table>` : `<div class="empty">还没有任务，点「新建任务」创建</div>`;
}

function openTaskForm(task) {
  const dlg = $("#task-dialog");
  const form = $("#task-form");
  form.reset();
  $("#extract-status").textContent = "";
  if (task) {
    $("#task-form-title").textContent = `编辑任务：${task.name}`;
    form.task_id.value = task.id;
    form.name.value = task.name;
    form.show_name.value = task.show_name;
    form.on_sale_at.value = task.on_sale_at;
    form.price_tiers.value = task.price_tiers.length === 1 ? task.price_tiers[0] : "";
    form.note.value = task.note || "";
    form.city.value = task.city || "";
    form.session.value = task.session || "";
    form.device_id.value = task.device_id || "";
    form.dataset.options = JSON.stringify(task.options || {});
    form.dataset.shareUrl = task.share_url || "";
    const opts = task.options || {};
    form.prewarm_minutes.value = opts.prewarm_minutes ?? "";
    form.arm_lead_seconds.value = opts.arm_lead_seconds ?? "";
    form.max_retry_seconds.value = opts.max_retry_seconds ?? "";
    form.max_attempts.value = opts.max_attempts ?? "";
  } else {
    $("#task-form-title").textContent = "新建抢票任务";
    form.task_id.value = "";
    form.dataset.options = "{}";
    form.dataset.shareUrl = "";
  }
  dlg.showModal();
}
function closeTaskForm() { $("#task-dialog").close(); }

async function submitTask(ev) {
  ev.preventDefault();
  const form = ev.target;
  const options = JSON.parse(form.dataset.options);
  for (const key of ["prewarm_minutes", "arm_lead_seconds", "max_retry_seconds", "max_attempts"]) {
    if (form[key].value !== "") options[key] = Number(form[key].value);
    else delete options[key];
  }
  const body = {
    name: form.name.value.trim(),
    show_name: form.show_name.value.trim(),
    on_sale_at: form.on_sale_at.value.trim(),
    price_tiers: [form.price_tiers.value.trim()],
    device_id: form.device_id.value ? Number(form.device_id.value) : null,
    options,
    note: form.note.value.trim(),
    city: form.city.value.trim(),
    session: form.session.value.trim(),
    share_url: form.dataset.shareUrl,
  };
  try {
    if (form.task_id.value) await api.patch(`/api/tasks/${form.task_id.value}`, body);
    else await api.post("/api/tasks", body);
    closeTaskForm();
    renderTasks();
  } catch (e) { alert(`保存失败：${e.message}`); }
  return false;
}

async function toggleTask(id, enabled) {
  const task = await api.get(`/api/tasks/${id}`);
  try {
    await api.patch(`/api/tasks/${id}`, {
      name: task.name, show_name: task.show_name, on_sale_at: task.on_sale_at,
      price_tiers: task.price_tiers, device_id: task.device_id,
      options: task.options, note: task.note, enabled: !!enabled,
      city: task.city, session: task.session, share_url: task.share_url,
    });
    renderTasks();
  } catch (e) { alert(e.message); }
}

async function deleteTask(id) {
  if (!(await confirm("删除该任务及其运行记录？"))) return;
  try { await api.del(`/api/tasks/${id}`); renderTasks(); }
  catch (e) { alert(e.message); }
}

async function runTask(id, dryRun) {
  const verb = dryRun ? "立即演练？会进入目标页识别可购按钮，不执行购买点击。"
    : "立即正式执行？会跳过定时，在目标详情页检测可购按钮。";
  if (!(await confirm(verb))) return;
  try {
    const { run_id } = await api.post(`/api/tasks/${id}/run`, { dry_run: dryRun });
    showRun(run_id);
  } catch (e) { alert(e.message); }
}

async function calibrateTask(id) {
  if (!(await confirm("立即校准？会从详情页进入「已预约」，读取已勾选的场次、票档及开售时间。"))) return;
  try {
    const r = await api.post(`/api/tasks/${id}/calibrate`);
    showRun(r.run_id);
  } catch (e) { alert(e.message); }
}

function openSchedule(id, dryRun) {
  const task = tasksCache.get(id);
  if (!task) return;
  const form = $("#schedule-form");
  form.task_id.value = id;
  form.dry_run.value = dryRun ? "1" : "0";
  const prewarm = task.prewarm_minutes;
  const updating = scheduleMode(task) === (dryRun ? "rehearsal" : "formal");
  const testTime = new Date(Date.now() + (prewarm + 1) * 60000 + 8 * 3600000)
    .toISOString().slice(0, 19).replace("T", " ");
  form.on_sale_at.readOnly = !dryRun;
  form.on_sale_at.value = dryRun ? (updating ? task.on_sale_at : testTime) : task.calibrated_at;
  const title = dryRun ? (updating ? "修改定时演练" : "建立定时演练") :
    (updating ? "修改正式排程" : "确认开抢时间");
  $("#schedule-title").textContent = `${title} · ${task.name}`;
  form.querySelector("[type=submit]").textContent = updating ? "保存排程修改" : "建立定时任务";
  $("#schedule-description").textContent = dryRun
    ? `提前 ${prewarm} 分钟预热，到点记录首击坐标和时间，跳过实际点击。`
    : `目标演出 ${task.calibration?.target_date || task.session}，票档 ${priceList(task)}。提前 ${prewarm} 分钟预热。页面开票时间 ${task.calibrated_at}。`;
  $("#schedule-error").textContent = "";
  $("#schedule-dialog").showModal();
}

async function submitSchedule(ev) {
  ev.preventDefault();
  const form = ev.target;
  const submit = form.querySelector("[type=submit]");
  const id = Number(form.task_id.value);
  const dryRun = form.dry_run.value === "1";
  const updating = scheduleMode(tasksCache.get(id)) === (dryRun ? "rehearsal" : "formal");
  submit.disabled = true;
  $("#schedule-error").textContent = "";
  try {
    const saved = await api.post(`/api/tasks/${id}/schedule`, {
      on_sale_at: form.on_sale_at.value.trim(), dry_run: dryRun,
    });
    ++tasksRequest; // 作废保存前发出的列表请求。
    renderTaskRows([...tasksCache.values()].map((task) =>
      task.id === id ? { ...task, ...saved } : task));
    const result = $("#task-schedule-result");
    result.textContent = `${saved.name} · ${dryRun ? "定时演练" : "正式排程"}已${updating ? "更新" : "建立"}。预热 ${saved.prewarm_at}，${dryRun ? "演练" : "开抢"} ${saved.on_sale_at}（北京时间）。`;
    result.hidden = false;
    $("#schedule-dialog").close();
  } catch (e) {
    $("#schedule-error").textContent = e.message;
  } finally { submit.disabled = false; }
}

async function cancelTask(id) {
  if (!(await confirm("停止该任务当前运行？"))) return;
  try { await api.post(`/api/tasks/${id}/cancel`); }
  catch (e) { alert(e.message); }
  refreshAll();
}

/* ---------- 截图提取 ---------- */

async function doExtract() {
  const input = $("#extract-file");
  const statusEl = $("#extract-status");
  const file = input.files[0];
  if (!file) { statusEl.textContent = "请先选择截图"; return; }
  statusEl.textContent = "本地模型识别中…（可能需要几十秒）";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await fetch("/api/extract", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.status);
    const draft = await resp.json();
    if (input.files[0] !== file || !$("#task-dialog").open) return;
    const form = $("#task-form");
    const fields = {
      name: draft.show_name,
      show_name: draft.show_name,
      city: draft.city,
      on_sale_at: draft.on_sale_guess,
      price_tiers: draft.price_guess,
      session: draft.session_guess,
    };
    for (const [key, value] of Object.entries(fields)) {
      const field = form.elements.namedItem(key);
      if (!field.value.trim() && value) field.value = value;
    }
    const missing = [
      !form.elements.namedItem("price_tiers").value.trim() && "预约票档",
      !form.elements.namedItem("session").value.trim() && "目标演出日期",
      !form.elements.namedItem("on_sale_at").value.trim() && "开票时间",
    ].filter(Boolean);
    const extra = [
      draft.full_title && `完整标题: ${draft.full_title}`,
      draft.sessions.length && `场次: ${draft.sessions.join(" / ")}`,
      draft.price_tiers.length && `可见票档: ${draft.price_tiers.join(" / ")} 元`,
      draft.on_sale_text && `开售文字: ${draft.on_sale_text}`,
      draft.venue && `场馆: ${draft.venue}`,
      draft.status_text && `按钮: ${draft.status_text}`,
    ].filter(Boolean).join("；");
    const review = missing.length ? `待核对填写：${missing.join("、")}` : "请核对已填内容";
    statusEl.textContent = `已提取（${draft.model}）${extra ? " · " + extra : ""}。 ${review}`;
  } catch (e) {
    if (input.files[0] === file && $("#task-dialog").open) {
      statusEl.textContent = `提取失败: ${e.message}`;
    }
  }
}

/* ---------- 设备 ---------- */

async function renderDevices() {
  const data = await api.get("/api/devices");
  devicesCache = data.devices;
  $("#device-list").innerHTML = data.devices.length ? `<table>
    <tr><th>Serial</th><th>型号</th><th>绑定账号</th><th>启用</th><th>在线</th><th>备注</th><th>操作</th></tr>
    ${data.devices.map((d) => `<tr>
      <td class="mono">${esc(d.serial)}</td><td>${esc(d.model || "")}</td>
      <td><select onchange="bindAccount(${d.id}, this.value)">
        <option value="">未绑定</option>
        ${accountsCache.map((a) =>
          `<option value="${a.id}" ${a.id === d.account_id ? "selected" : ""}>${esc(a.label)}</option>`).join("")}
      </select></td>
      <td>${d.enabled ? "✅" : "⛔"}</td>
      <td>${d.online ? "✅" : "❌"}</td>
      <td>${esc(d.note || "")}</td>
      <td><div class="row-actions">
        <button class="small" onclick="showDeviceLoginConfig(${d.id})">登录配置</button>
        <button class="small" onclick="toggleDevice(${d.id}, ${d.enabled ? 0 : 1})">${d.enabled ? "禁用" : "启用"}</button>
        <button class="small danger" onclick="deleteDevice(${d.id})">删除</button>
      </div></td>
    </tr>`).join("")}</table>` : `<div class="empty">暂无注册设备</div>`;

  const online = data.online.filter((o) => !data.devices.some((d) => d.serial === o.serial));
  $("#device-unregistered").innerHTML = online.length ? `<table>
    <tr><th>Serial</th><th>型号</th><th></th></tr>
    ${online.map((o) => `<tr>
      <td class="mono">${esc(o.serial)}</td><td>${esc(o.model || "")}</td>
      <td><button class="small primary" onclick="registerDevice('${esc(o.serial)}', '${esc(o.model || "")}')">注册</button></td>
    </tr>`).join("")}</table>` : `<div class="empty">没有未注册的在线设备</div>`;
}

async function registerDevice(serial, model) {
  // 直接注册；账号绑定用列表里的下拉框完成
  try {
    await api.post("/api/devices", { serial, model, account_id: null });
    renderDevices();
  } catch (e) { alert(e.message); }
}
async function bindAccount(deviceId, accountId) {
  try { await api.patch(`/api/devices/${deviceId}`, { account_id: accountId ? Number(accountId) : null }); }
  catch (e) { alert(e.message); }
}
async function toggleDevice(id, enabled) {
  await api.patch(`/api/devices/${id}`, { enabled: !!enabled });
  renderDevices();
}
async function deleteDevice(id) {
  if (!(await confirm("删除设备注册记录？（不影响手机）"))) return;
  await api.del(`/api/devices/${id}`);
  renderDevices();
}

/* ---------- 大麦登录 ---------- */

const deviceLoginResults = new Map();
const loginDialog = $("#device-login-dialog");
const loginForm = $("#device-login-form");
loginDialog.addEventListener("close", () => loginForm.reset());

async function renderLoginDevices() {
  const { devices } = await api.get("/api/devices");
  $("#login-device-list").innerHTML = devices.length ? `<table>
    <tr><th>设备</th><th>在线</th><th>操作</th><th>登录结果</th></tr>
    ${devices.map((d) => {
      const result = deviceLoginResults.get(d.id);
      return `<tr>
        <td>${esc(d.model || d.serial)}${d.model ? ` <span class="muted mono">${esc(d.serial)}</span>` : ""}</td>
        <td>${d.online ? "在线" : "离线"}</td>
        <td><div class="row-actions">
          <button class="small" onclick="openDeviceLoginConfig(${d.id})">配置</button>
          <button class="small primary" id="device-login-btn-${d.id}" onclick="startDeviceLogin(${d.id})"
            data-available="${Boolean(d.online && d.enabled)}"
            ${d.online && d.enabled && !result?.pending ? "" : "disabled"}>登录</button>
        </div></td>
        <td id="device-login-result-${d.id}" role="status">${esc(result?.message || "")}</td>
      </tr>`;
    }).join("")}</table>` : '<div class="empty">请先在「设备」页注册 ADB 设备</div>';
}

function showDeviceLoginConfig(id) {
  $('#nav button[data-tab="logins"]').click();
  openDeviceLoginConfig(id);
}

async function openDeviceLoginConfig(id) {
  try {
    const config = await api.get(`/api/devices/${id}/login`);
    loginForm.reset();
    loginForm.device_id.value = id;
    $("#device-login-error").textContent = "";
    $("#device-login-hint").textContent = config.configured
      ? `已保存尾号 ${config.phone_tail} 的登录配置。填写手机号和密码可替换。`
      : "手机号和密码保存在本机 macOS 钥匙串。";
    $("#clear-device-login").hidden = !config.configured;
    if (!loginDialog.open) loginDialog.showModal();
  } catch (e) { alert(e.message); }
}

async function saveDeviceLogin(ev) {
  ev.preventDefault();
  const id = loginForm.device_id.value;
  const button = loginForm.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await api.put(`/api/devices/${id}/login`, {
      phone: loginForm.phone.value.trim(),
      password: loginForm.password.value,
    });
    loginDialog.close();
    showDeviceLoginResult(id, "登录配置已保存");
  } catch (e) {
    $("#device-login-error").textContent = e.message;
  } finally {
    button.disabled = false;
  }
  return false;
}

async function clearDeviceLogin() {
  const id = loginForm.device_id.value;
  try {
    await api.del(`/api/devices/${id}/login`);
    loginDialog.close();
    showDeviceLoginResult(id, "已清除登录配置");
  } catch (e) {
    $("#device-login-error").textContent = e.message;
  }
}

function showDeviceLoginResult(id, message, pending = false) {
  deviceLoginResults.set(Number(id), { message, pending });
  const status = $(`#device-login-result-${id}`);
  if (status) status.textContent = message;
}

async function startDeviceLogin(id) {
  const button = $(`#device-login-btn-${id}`);
  if (deviceLoginResults.get(id)?.pending) return;
  if (button) button.disabled = true;
  showDeviceLoginResult(id, "正在登录，请保持手机解锁…", true);
  try {
    const result = await api.post(`/api/devices/${id}/login`);
    showDeviceLoginResult(id, result.status === "already_signed_in" ? "设备已有登录会话" : "登录成功");
  } catch (e) {
    showDeviceLoginResult(id, e.message);
  } finally {
    const current = $(`#device-login-btn-${id}`);
    if (current) current.disabled = current.dataset.available !== "true";
  }
}

/* ---------- 账号 ---------- */

async function renderAccounts() {
  accountsCache = await api.get("/api/accounts");
  $("#account-list").innerHTML = accountsCache.length ? `<table>
    <tr><th>账号</th><th>备注</th><th>操作</th></tr>
    ${accountsCache.map((a) => `<tr>
      <td>${esc(a.label)}</td><td>${esc(a.note || "")}</td>
      <td><div class="row-actions">
        <button class="small" onclick="editAccount(${a.id})">编辑</button>
        <button class="small danger" onclick="deleteAccount(${a.id})">删除</button>
      </div></td>
    </tr>`).join("")}</table>` : `<div class="empty">暂无账号</div>`;
}
function openAccountForm(account) {
  const dlg = $("#account-dialog");
  const form = $("#account-form");
  form.reset();
  if (account) {
    $("#account-form-title").textContent = `编辑账号：${account.label}`;
    form.account_id.value = account.id;
    form.label.value = account.label;
    form.note.value = account.note || "";
  } else {
    $("#account-form-title").textContent = "新建账号";
    form.account_id.value = "";
  }
  dlg.showModal();
}
function closeAccountForm() { $("#account-dialog").close(); }

async function submitAccount(ev) {
  ev.preventDefault();
  const form = ev.target;
  const body = { label: form.label.value.trim(), note: form.note.value.trim() };
  try {
    if (form.account_id.value) await api.patch(`/api/accounts/${form.account_id.value}`, body);
    else await api.post("/api/accounts", body);
    closeAccountForm();
    renderAccounts();
  } catch (e) { alert(`保存失败：${e.message}`); }
  return false;
}

async function editAccount(id) {
  const acc = accountsCache.find((a) => a.id === id);
  if (acc) openAccountForm(acc);
}
async function deleteAccount(id) {
  if (!(await confirm("删除账号标注？"))) return;
  await api.del(`/api/accounts/${id}`);
  renderAccounts();
}

/* ---------- 运行记录 ---------- */

async function renderRuns() {
  const runs = await api.get("/api/runs?limit=100");
  $("#run-list").innerHTML = runs.length ? `<table>
    <tr><th>#</th><th>任务</th><th>开始</th><th>结束</th><th>结果</th><th>尝试</th><th></th></tr>
    ${runs.map((r) => `<tr>
      <td class="mono">${r.id}</td><td>${esc(r.task_name)}</td>
      <td class="mono">${esc(r.started_at)}</td>
      <td class="mono">${esc(r.finished_at || "")}</td>
      <td>${badge(r.outcome || "running", OUTCOME_LABEL[r.outcome] || r.outcome || "运行中")}
        ${r.dry_run ? badge("dry", "演练") : ""}</td>
      <td class="mono">${r.attempts}</td>
      <td><button class="small" onclick="showRun(${r.id})">详情</button></td>
    </tr>`).join("")}</table>` : `<div class="empty">暂无运行记录</div>`;
}

async function showRun(runId) {
  const data = await api.get(`/api/runs/${runId}`);
  const dlg = document.createElement("dialog");
  function render(data) {
    dlg.innerHTML = `
    <h3>运行 #${data.run.id} · ${esc(data.run.task_name)}
      ${badge(data.run.outcome || "running", OUTCOME_LABEL[data.run.outcome] || data.run.outcome || "运行中")}
      ${data.run.dry_run ? badge("dry", "演练") : ""}</h3>
    <p class="muted">${esc(data.run.started_at)} → ${esc(data.run.finished_at || "…")} · 尝试 ${data.run.attempts} 次</p>
    ${data.recording_url ? `<section class="run-recording">
      <h4>抢票录屏</h4>
      <video controls playsinline preload="metadata" src="${esc(data.recording_url)}"></video>
      <a href="${esc(data.recording_url)}" download>下载 MP4</a>
    </section>` : !data.run.dry_run ? `<p class="muted">${data.run.finished_at
      ? "本次运行没有保存录屏" : "录屏在运行结束后可回放"}</p>` : ""}
    <ul class="timeline">${data.events.map((e) =>
      `<li class="${e.level}"><time>${esc(e.ts.slice(11))}</time>${esc(e.message)}</li>`).join("")}</ul>
    <div class="dialog-actions"><button onclick="this.closest('dialog').close()">关闭</button></div>`;
  }
  async function follow() {
    if (!dlg.open) return;
    try {
      const current = await api.get(`/api/runs/${runId}`);
      if (!dlg.open) return;
      render(current);
      if (current.run.finished_at) return;
    } catch (e) {
      console.error("运行记录刷新失败", e);
    }
    setTimeout(follow, 2000);
  }
  render(data);
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.addEventListener("close", () => dlg.remove());
  if (!data.run.finished_at) setTimeout(follow, 2000);
}

async function showTaskEvents(taskId) {
  const events = await api.get(`/api/tasks/${taskId}/events`);
  const dlg = document.createElement("dialog");
  dlg.innerHTML = `
    <h3>任务日志（最近 300 条）</h3>
    <ul class="timeline">${events.length ? events.map((e) =>
      `<li class="${e.level}"><time>${esc(e.ts.slice(5))}</time>${esc(e.message)}</li>`).join("")
      : '<li>暂无日志</li>'}</ul>
    <div class="dialog-actions"><button onclick="this.closest('dialog').close()">关闭</button></div>`;
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.addEventListener("close", () => dlg.remove());
}

/* ---------- 设置 ---------- */

async function renderSettings() {
  const s = await api.get("/api/overview");
  const st = s.settings;
  const form = $("#settings-form");
  form.clock_offset_ms.value = st.clock_offset_ms;
  form.ollama_url.value = st.ollama_url;
  form.vlm_model.value = st.vlm_model;
  form.default_prewarm_minutes.value = st.default_prewarm_minutes;
  $("#vlm-models").innerHTML = (s.extractor.models || [])
    .map((m) => `<option value="${esc(m)}">`).join("");
  $("#extractor-state").textContent = s.extractor.reachable
    ? `Ollama 可达，可用模型 ${s.extractor.models.length} 个，当前使用: ${s.extractor.model || "未选择"}`
    : `Ollama 不可达（${s.extractor.error || ""}），截图提取不可用`;
}

async function saveSettings(ev) {
  ev.preventDefault();
  const form = ev.target;
  await api.put("/api/settings", {
    clock_offset_ms: Number(form.clock_offset_ms.value || 0),
    ollama_url: form.ollama_url.value.trim(),
    vlm_model: form.vlm_model.value.trim(),
    default_prewarm_minutes: Number(form.default_prewarm_minutes.value || 5),
  });
  $("#extractor-state").textContent = "已保存";
  return false;
}

/* ---------- 刷新循环 ---------- */

async function refreshAll() {
  const active = document.querySelector(".tab.active").id.replace("tab-", "");
  try {
    if (active === "overview") await renderOverview();
    else if (active === "tasks") await renderTasks();
    else if (active === "devices") { await renderAccounts(); await renderDevices(); }
    else if (active === "logins") await renderLoginDevices();
    else if (active === "accounts") await renderAccounts();
    else if (active === "runs") await renderRuns();
    else if (active === "settings") await renderSettings();
  } catch (e) {
    console.error("刷新失败", e);
  }
}

setInterval(updateCountdowns, 250);
setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);
setInterval(() => {
  const active = document.querySelector(".tab.active").id.replace("tab-", "");
  if (active === "overview" || active === "tasks") refreshAll();
}, 3000);
refreshAll();
