/* 灵语 - 前端逻辑（pywebview JS API） */

let draft = null;       // 当前编辑中的配置
let lastLogSeq = 0;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------------- 导航 ----------------
$$("#nav .nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    $$("#nav .nav-item").forEach((n) => n.classList.remove("active"));
    $$(".page").forEach((p) => p.classList.remove("active"));
    item.classList.add("active");
    $("#page-" + item.dataset.page).classList.add("active");
    // 情绪是「观测」页：切进来时按需拉一次，避免常驻轮询读状态文件。
    if (item.dataset.page === "emotion") {
      loadEmotion();
      loadPersona();
    }
  });
});

// ---------------- 数据绑定 ----------------
// range 滑块的数值标签：由 data-val 指向同页的 <span>，避免写死单个 id。
function rangeLabel(el) {
  const id = el.dataset.val;
  return id ? document.getElementById(id) : null;
}

function fmtRange(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : "";
}

function bindDraftToControls() {
  $$("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    if (el.type === "checkbox") el.checked = !!draft[key];
    else el.value = draft[key] ?? "";
    if (el.type === "range") {
      const label = rangeLabel(el);
      if (label) label.textContent = fmtRange(draft[key]);
    }

    const onEdit = () => {
      if (el.type === "checkbox") draft[key] = el.checked;
      else if (el.type === "range") {
        draft[key] = parseFloat(el.value);
        const label = rangeLabel(el);
        if (label) label.textContent = fmtRange(el.value);
      } else draft[key] = el.value;
      if (key === "wechat_enabled" || key === "qq_enabled") {
        // 至少保留一个终端，否则保存后没有任何适配器可接消息。
        if (!draft.wechat_enabled && !draft.qq_enabled) {
          draft[key] = true;
          el.checked = true;
          toast("至少保留一个接入终端", "err");
          return;
        }
        syncPlatformFields();
        // 终端开关本身就是「应用」动作：勾上就立即保存并按新开关重连，
        // 不需要用户再点一次「保存并应用」。
        saveAll().catch((err) => toast("应用终端失败：" + err, "err"));
      }
    };
    // 用属性赋值而不是 addEventListener：这个函数每次保存后都会重跑，
    // 用监听器会越绑越多，一次改动触发多次保存。
    el.oninput = onEdit;
    // <select> 在部分 WebView 上不派发 input，补一个 change 兜住。
    if (el.tagName === "SELECT") el.onchange = onEdit;
  });

  syncPlatformFields();

  // 作用范围 segmented
  const scope = draft.all_groups ? "all" : "custom";
  $$(".seg").forEach((b) => b.classList.toggle("active", b.dataset.scope === scope));
  $("#groups-field").style.display = draft.all_groups ? "none" : "block";
  $("#groups-input").value = (draft.groups || []).join("\n");

  // 撤回指令的允许成员
  $("#recall-admins-input").value = (draft.recall_admins || []).join("\n");
}

function collectDraft() {
  // segmented / textarea 的值回收到 draft
  draft.all_groups = $$(".seg").find((b) => b.classList.contains("active")).dataset.scope === "all";
  draft.groups = $("#groups-input").value.split("\n").map((s) => s.trim()).filter(Boolean);
  draft.recall_admins = $("#recall-admins-input").value
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  return draft;
}

$$(".seg").forEach((b) => {
  b.addEventListener("click", () => {
    $$(".seg").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#groups-field").style.display = b.dataset.scope === "custom" ? "block" : "none";
  });
});

// ---------------- 保存 ----------------
async function saveAll() {
  collectDraft();
  const state = await window.pywebview.api.save_config(draft);
  draft = state.config;
  bindDraftToControls();
  renderStatus(state);
  const conn = state.connect;
  const failed = ((conn && conn.adapters) || []).filter((a) => !a.ok);
  if (failed.length) {
    const detail = failed
      .map((a) => `${a.label || a.platform}：${a.error?.message || "连接失败"}`)
      .join("；");
    toast("已保存，部分终端连接失败：" + detail, "err");
  } else if (state.applied) {
    toast("已切换并连接");
  } else {
    toast("设置已保存");
  }
}
$("#save-chats").addEventListener("click", saveAll);
$("#save-links").addEventListener("click", saveAll);
$("#save-recall").addEventListener("click", saveAll);
$("#save-model").addEventListener("click", saveAll);
$("#save-platform").addEventListener("click", saveAll);

// 接入终端：QQ 字段按开关显隐。
function syncPlatformFields() {
  const isQQ = !!draft && !!draft.qq_enabled;
  const box = $("#qq-fields");
  if (box) box.style.display = isQQ ? "block" : "none";
  const test = $("#test-qq");
  if (test) test.style.display = isQQ ? "" : "none";
}

// 绑定状态：每个终端一行（微信 / QQ 可同时在跑）。
function renderBinding(state) {
  const b = state.binding;
  if (!b) return;
  const entries = b.entries && b.entries.length ? b.entries : [b];
  const text = entries
    .map((item) => {
      const name = item.label || item.platform;
      const who = item.bot_name || item.bot_id;
      const stateText = item.ok
        ? "已连接"
        : (item.error?.message || "未连接");
      return [name, who, stateText].filter(Boolean).join(" · ");
    })
    .join(" ｜ ");
  $("#s-binding").textContent = text || "未启用任何终端";
  // 密钥不回填明文：已保存时用占位提示「留空沿用」。
  const secret = $('input[data-key="qq_app_secret"]');
  if (secret) {
    secret.placeholder = b.secret_set
      ? "已保存密钥；留空表示沿用，重新填写可覆盖"
      : "在 q.qq.com 开放平台「开发设置」中获取";
  }
}

$("#test-qq").addEventListener("click", async () => {
  collectDraft();
  const btn = $("#test-qq");
  btn.disabled = true;
  try {
    const r = await window.pywebview.api.test_qq_binding(draft);
    if (r.ok) {
      toast("凭据有效：" + (r.gateway ? "已取得网关，可以保存并应用" : "可以保存并应用"));
    } else {
      toast("测试失败：" + (r.error?.message || r.error?.code || ""), "err");
    }
  } finally {
    btn.disabled = false;
  }
});
$("#save-emotion").addEventListener("click", async () => {
  await saveAll();
  loadEmotion();
  loadPersona();
});

// ---------------- 总开关 / 连接 ----------------
$("#btn-toggle").addEventListener("click", async () => {
  collectDraft();
  draft.enabled = !draft.enabled;
  const state = await window.pywebview.api.save_config(draft);
  draft = state.config;
  bindDraftToControls();
  renderStatus(state);
  toast(draft.enabled ? "自动回复已开启" : "自动回复已停止");
});

$("#btn-connect").addEventListener("click", async () => {
  $("#btn-connect").disabled = true;
  const result = await window.pywebview.api.connect_platform();
  $("#btn-connect").disabled = false;
  const failed = (result.adapters || []).filter((a) => !a.ok);
  if (failed.length) {
    const detail = failed
      .map((a) => `${a.label || a.platform}：${a.error?.message || "连接失败"}`)
      .join("；");
    toast("连接失败：" + detail, "err");
  } else {
    toast(result.adapters?.length > 1 ? "所有终端已连接" : "已连接");
  }
  refreshState();
});

$("#btn-test").addEventListener("click", async () => {
  collectDraft();
  $("#btn-test").disabled = true;
  const r = await window.pywebview.api.test_connection(draft);
  $("#btn-test").disabled = false;
  toast(r.ok ? "连接正常：" + r.reply.slice(0, 40) : "测试失败：" + r.error.message, r.ok ? null : "err");
});

// ---------------- 情绪 ----------------
// 身份清单要在 bindDraftToControls 之前灌进 <select>，否则下拉框没有 option，
// 绑定上去的 emotion_identity 会显示成空白。
async function loadIdentities() {
  let list = [];
  try {
    list = await window.pywebview.api.list_identities();
  } catch (e) {
    list = [];
  }
  const sel = $("#identity-select");
  sel.innerHTML = "";
  (list || []).forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.name || item.id;
    sel.appendChild(opt);
  });
  if (!sel.options.length) {
    const opt = document.createElement("option");
    opt.value = "violet";
    opt.textContent = "薇尔莉特";
    sel.appendChild(opt);
  }
}

function fmtNum(value, digits) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits === undefined ? 2 : digits) : "—";
}

// 兴趣：本轮话题 + 演化后的前几名。它不是配置，所以只读展示。
function renderInterest(interest) {
  if (!interest) return "";
  const top = (interest.top || []).slice(0, 3);
  const topic = interest.topic
    ? '<span class="emotion-topic">话题 ' + escapeHtml(String(interest.topic)) +
      " · 注意 " + fmtNum(Number(interest.attention) || 0) + "</span>"
    : '<span class="emotion-topic muted">这轮没有具体话题</span>';
  const chips = top
    .map((v) =>
      '<span class="emotion-chip">' + escapeHtml(String(v.name || v.id || "")) +
      " " + fmtNum(Number(v.value) || 0) + "</span>")
    .join("");
  return '<div class="emotion-interest">' + topic + chips + "</div>";
}

// 心境与关系只是观测：不参与配置，也不写回。
function renderChips(pairs) {
  const chips = pairs
    .filter((item) => item[1] !== undefined && item[1] !== null)
    .map((item) =>
      '<span class="emotion-chip">' + item[0] + " " +
      fmtNum(Number(item[1]) || 0) + "</span>")
    .join("");
  return chips ? '<div class="emotion-interest">' + chips + "</div>" : "";
}

function renderEmotion(payload) {
  const box = $("#emotion-list");
  const chats = (payload && payload.chats) || [];
  box.innerHTML = "";
  if (!chats.length) {
    box.innerHTML =
      '<div class="emotion-empty">还没有会话产生过心境。收到消息后会出现在这里。</div>';
  }
  chats.forEach((item) => {
    const intensity = Number(item.intensity) || 0;
    const tag = item.dominant
      ? item.dominant + " · " + fmtNum(intensity)
      : "平静";
    const status = item.silent ? "选择了沉默" : "已表达";
    const meta = [
      "第 " + (item.turn || 0) + " 轮",
      item.kind ? "本轮 " + item.kind : "",
      status,
      item.source ? "来源 " + item.source : "",
      item.reason || "",
    ]
      .filter(Boolean)
      .join(" · ");
    const pct = Math.max(0, Math.min(100, Math.round(intensity * 100)));
    const relation = item.relation || {};
    const affect = item.affect || {};
    const row = document.createElement("div");
    row.className = "emotion-row";
    row.innerHTML =
      '<div class="emotion-head"><span class="emotion-name">' +
      escapeHtml(String(item.chat || "—")) +
      '</span><span class="emotion-tag' +
      (item.silent ? " muted" : "") +
      '">' +
      escapeHtml(tag) +
      "</span></div>" +
      '<div class="emotion-meta">' +
      escapeHtml(meta) +
      "</div>" +
      '<div class="emotion-bar"><i style="width:' +
      pct +
      '%"></i></div>' +
      (item.memo
        ? '<div class="emotion-meta">' + escapeHtml(String(item.memo)) + "</div>"
        : "") +
      renderChips([
        ["熟悉", relation.familiarity],
        ["信任", relation.trust],
        ["亲近", relation.closeness],
        ["紧张", relation.tension],
      ]) +
      renderChips([
        ["心情", affect.valence],
        ["唤起", affect.arousal],
        ["想念", affect.longing],
        ["烦躁", affect.irritation],
        ["精力", affect.energy],
      ]) +
      renderInterest(item.interest);
    box.appendChild(row);
  });
  const stats = (payload && payload.stats) || {};
  $("#emotion-dir").textContent = stats.dir ? "状态目录：" + stats.dir : "—";
}

async function loadEmotion() {
  const payload = await window.pywebview.api.get_emotion_state(null);
  renderEmotion(payload);
}

$("#btn-refresh-emotion").addEventListener("click", loadEmotion);

$("#btn-reset-emotion").addEventListener("click", async () => {
  const r = await window.pywebview.api.reset_emotion(null);
  renderEmotion(r.state);
  toast("已清空 " + (r.removed || 0) + " 个会话的心境");
});

// ---------------- 人格札记 ----------------
const PERSONA_STATUS = {
  active: "有效",
  superseded: "已修订",
  retracted: "已撤回",
  archived: "已归档",
};

function renderPersona(payload) {
  const box = $("#persona-list");
  const entries = (payload && payload.entries) || [];
  box.innerHTML = "";
  if (!payload || !payload.enabled) {
    box.innerHTML =
      '<div class="emotion-empty">人格札记已关闭。打开上面的开关后，她会在证据足够时写下第一条。</div>';
  } else if (!entries.length) {
    box.innerHTML =
      '<div class="emotion-empty">还没有札记。证据不足时她会选择不写，这是允许的。</div>';
  }
  entries.slice().reverse().forEach((entry) => {
    const status = PERSONA_STATUS[entry.status] || entry.status || "—";
    const sources = (entry.sources || []).length;
    const meta = [
      entry.created_day || "",
      "置信 " + fmtNum(Number(entry.confidence) || 0),
      "来源 " + sources,
      entry.uncertainty ? "不确定 " + fmtNum(Number(entry.uncertainty) || 0) : "",
      entry.revision_of ? "修订自 " + entry.revision_of : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const row = document.createElement("div");
    row.className = "emotion-row";
    row.dataset.entry = entry.id;
    const actions = entry.status === "active"
      ? '<div class="emotion-interest">' +
        '<button class="btn ghost xs" data-persona="revise">修订</button>' +
        '<button class="btn ghost xs" data-persona="retract">撤回</button></div>'
      : "";
    row.innerHTML =
      '<div class="emotion-head"><span class="emotion-name">' +
      escapeHtml(String(entry.text || "—")) +
      '</span><span class="emotion-tag' +
      (entry.status === "active" ? "" : " muted") +
      '">' +
      escapeHtml(status) +
      "</span></div>" +
      '<div class="emotion-meta">' + escapeHtml(meta) + "</div>" +
      (entry.counterexample
        ? '<div class="emotion-meta">反例：' +
          escapeHtml(String(entry.counterexample)) + "</div>"
        : "") +
      actions;
    box.appendChild(row);
  });
  const stats = (payload && payload.stats) || {};
  $("#persona-stats").textContent = stats.path
    ? "账本：" + stats.path + "｜共 " + (stats.total || 0) +
      " 条｜最近 " + (stats.last_day || "—")
    : "—";
}

async function loadPersona() {
  let payload = null;
  try {
    payload = await window.pywebview.api.get_persona(null);
  } catch (e) {
    payload = null;
  }
  renderPersona(payload);
}

$("#btn-refresh-persona").addEventListener("click", loadPersona);

$("#persona-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-persona]");
  if (!btn) return;
  const row = btn.closest("[data-entry]");
  const entryId = row && row.dataset.entry;
  if (!entryId) return;

  if (btn.dataset.persona === "revise") {
    const current = (row.querySelector(".emotion-name") || {}).textContent || "";
    const text = window.prompt("修订这条札记（4~60 字）", current);
    if (text === null) return;
    const trimmed = text.trim();
    if (trimmed.length < 4 || trimmed.length > 60) {
      toast("札记需 4~60 字", "err");
      return;
    }
    const r = await window.pywebview.api.revise_persona(entryId, trimmed, null);
    renderPersona(r.state);
    toast(r.entry ? "已修订" : "修订失败", r.entry ? null : "err");
    return;
  }

  if (btn.dataset.persona === "retract") {
    if (!window.confirm("撤回后这条不会再进入表达提示词，确定吗？")) return;
    const r = await window.pywebview.api.retract_persona(entryId, "", null);
    renderPersona(r.state);
    toast(r.entry ? "已撤回" : "撤回失败", r.entry ? null : "err");
  }
});

$("#btn-reset-persona").addEventListener("click", async () => {
  if (!window.confirm("清空后所有札记都会消失，确定吗？")) return;
  const r = await window.pywebview.api.reset_persona(null);
  renderPersona(r.state);
  toast(r.ok ? "已清空人格札记" : "清空失败", r.ok ? null : "err");
});

// ---------------- 状态渲染 ----------------
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function renderStatus(state) {
  const engine = state.engine;
  // 兼容旧版本（wechat）与多端版本（adapters）。
  const w = engine.adapter || engine.wechat;
  const list = engine.adapters && engine.adapters.length ? engine.adapters : [w];
  const connected = list.filter((x) => x.ok);
  const allOk = list.length > 0 && connected.length === list.length;
  const platform =
    list.length > 1 ? "微信 + QQ" : (list[0]?.platform === "qq" ? "QQ" : "微信");

  $("#s-wx").textContent =
    list.length > 1
      ? `已连接 ${connected.length}/${list.length}`
      : (w.ok ? "已连接" : "未连接");
  $("#s-listen").textContent = w.listening ? "监听中" : "未监听";
  $("#s-bot").textContent = engine.running ? "运行中" : "已停止";
  const btn = $("#btn-toggle");
  btn.textContent = engine.running ? "停止" : "开启";
  btn.classList.toggle("primary", !engine.running);
  btn.style.background = engine.running ? "#fff" : "";
  btn.style.color = engine.running ? "#ef4444" : "";
  btn.style.borderColor = engine.running ? "#fecaca" : "";

  $("#s-count").textContent = engine.reply_count_today;
  $("#s-last").textContent = fmtTime(engine.last_trigger_at);

  // 侧栏底部
  const dot = $("#wx-dot");
  dot.className = "status-dot " + (allOk ? "ok" : "err");
  $("#wx-title").textContent = allOk ? platform + "已连接" : "未连接" + platform;
  $("#wx-sub").textContent =
    list.length > 1
      ? connected.length === list.length
        ? "所有终端在线"
        : `在线：${connected.map((x) => (x.platform === "qq" ? "QQ" : "微信")).join("、") || "无"}`
      : (w.backend ? "后端：" + w.backend : (w.error?.message || "—"));

  renderBinding(state);
}

async function refreshState() {
  const state = await window.pywebview.api.get_state();
  // 不覆盖用户正在编辑的草稿，只刷新运行状态
  renderStatus(state);
}

// ---------------- 日志 ----------------
async function pollLogs() {
  const lines = await window.pywebview.api.get_logs(lastLogSeq);
  if (!lines.length) return;
  const box = $("#log-box");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const frag = document.createDocumentFragment();
  lines.forEach((l) => {
    lastLogSeq = l.seq;
    const div = document.createElement("div");
    div.className = "log-line log-" + l.level;
    const t = new Date(l.timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    div.innerHTML = '<span class="log-time">' + t + "</span>" + escapeHtml(l.message);
    frag.appendChild(div);
  });
  box.appendChild(frag);
  if ($("#log-follow").checked && atBottom) box.scrollTop = box.scrollHeight;
}

$("#btn-clear-log").addEventListener("click", () => { $("#log-box").innerHTML = ""; });

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// ---------------- Toast ----------------
let toastTimer = null;
function toast(msg, kind) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (kind === "err" ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

// ---------------- 启动 ----------------
async function init() {
  await loadIdentities();
  const state = await window.pywebview.api.get_state();
  draft = state.config;
  bindDraftToControls();
  renderStatus(state);
  loadEmotion();
  loadPersona();
  lastLogSeq = 0;
  $("#log-box").innerHTML = "";
  pollLogs();
  setInterval(refreshState, 3000);
  setInterval(pollLogs, 1500);
}

window.addEventListener("pywebviewready", init);
