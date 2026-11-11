"use strict";

const STATUS_TEXT = {
  queued: "排隊中",
  running: "生成中",
  completed: "已完成",
  failed: "失敗",
  cancelled: "已取消",
};

let configured = false;
let jobs = [];
let currentJobId = null;
let previewJob = null;
let displayedJob = null;
let authed = false;
let refreshTimer = null;
let servers = [];
let currentServerId = null;
let editingServerId = null;
// 歷史頁的伺服器篩選：預設跟著目前伺服器走（只看這個伺服器的任務），
// 選 "all" 可以兩個伺服器一起看。切換伺服器時自動跟著切。
let historyServerFilter = "current";
// 圖片模糊（公共場合防偷看）：每張圖預設全糊，各自帶開關。
// 點一張糊著的圖（或它旁邊的開關）看那張的原圖，再點一下糊回去。
// 哪張看過了只記在這次打開的頁面裡，刷新就回到全糊。
let revealedJobs = new Set();

function applyImgBlur(imgEl, jobId) {
  if (!imgEl) return false;
  const blurred = !!jobId && !revealedJobs.has(jobId);
  imgEl.classList.toggle("img-blurred", blurred);
  return blurred;
}

function toggleReveal(jobId) {
  if (!jobId) return;
  if (revealedJobs.has(jobId)) revealedJobs.delete(jobId);
  else revealedJobs.add(jobId);
  renderCurrentJob();
  renderHistory();
}

function $(id) {
  return document.getElementById(id);
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function fmtSeconds(s) {
  if (s === null || s === undefined) return "—";
  return Math.round(s) + " 秒";
}

function fmtUptime(s) {
  if (s === null || s === undefined) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小時`;
  if (h > 0) return `${h} 小時 ${m} 分`;
  return `${m} 分`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 帳號脫敏（公共場合防偷看）：只顯示頭一個字母，後面全部打星號，
// 例如 xiongyong5207 → x***********。數據集只把 owner 那段打碼，
// slug 原樣保留，例如 xiongyong5207/qwen-image-21-models →
// x***********/qwen-image-21-models。
function maskName(name) {
  const s = String(name || "");
  if (s.length <= 1) return s;
  return s[0] + "*".repeat(s.length - 1);
}

function maskDataset(ref) {
  const s = String(ref || "");
  if (!s) return s;
  const parts = s.split("/");
  parts[0] = maskName(parts[0]);
  return parts.join("/");
}

// ---- 頁籤 ----
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  });
});

function showTab(name) {
  document.querySelector(`.tab[data-tab="${name}"]`).click();
}

// ---- 認證：帳號密碼 ----
function showAuthScreen() {
  $("authScreen").classList.remove("hidden");
}

function hideAuthScreen() {
  $("authScreen").classList.add("hidden");
}

async function checkAuth() {
  try {
    const r = await fetch("/api/auth/status");
    const s = await r.json();
    if (!s.authenticated) {
      showLogin();
      return;
    }
    enterApp();
  } catch (e) {
    showLogin();
  }
}

function showLogin() {
  authed = false;
  stopRefresh();
  showAuthScreen();
  $("loginMsg").textContent = "";
  $("loginPass").value = "";
  $("loginUser").focus();
}

function enterApp() {
  authed = true;
  hideAuthScreen();
  loadSettings();
  loadServers();
  loadQuota();
  loadStatus();
  refreshJobs();
  startRefresh();
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    refreshJobs();
    loadQuota();
    loadStatus();
  }, 10000);
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

$("loginBtn").addEventListener("click", async () => {
  const username = $("loginUser").value.trim();
  const password = $("loginPass").value;
  const msg = $("loginMsg");
  if (!username || !password) {
    msg.textContent = "請輸入帳號和密碼。";
    return;
  }
  msg.textContent = "登入中……";
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await r.json();
  if (r.ok && data.ok) {
    $("loginPass").value = "";
    enterApp();
  } else {
    msg.textContent = "登入失敗：" + (data.error || "請稍後再試。");
  }
});

function submitOnEnter(input, btn) {
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      btn.click();
    }
  });
}
submitOnEnter($("loginUser"), $("loginBtn"));
submitOnEnter($("loginPass"), $("loginBtn"));
submitOnEnter($("confirmPass"), $("changePassBtn"));

function truncateText(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n) + "……" : s;
}

// ---- 設定：Kaggle ----
async function loadSettings() {
  try {
    const r = await fetch("/api/settings");
    if (r.status === 401) { showLogin(); return; }
    const s = await r.json();
    configured = !!s.kaggle_configured;
    $("keyWarning").classList.toggle("hidden", configured);
  } catch (e) {
    configured = false;
  }
}

async function loadQuota() {
  try {
    const r = await fetch("/api/quota");
    if (r.status === 401) { showLogin(); return; }
    const q = await r.json();
    $("quotaText").textContent =
      `已用 ${fmtSeconds(q.used_seconds)}／每週 ${fmtSeconds(q.total_seconds)}，` +
      `還剩 ${fmtSeconds(q.remaining_seconds)}。每週額度由 Kaggle 刷新。`;
  } catch (e) {
    $("quotaText").textContent = "額度讀取失敗，請稍後再試。";
  }
}

$("testBtn").addEventListener("click", async () => {
  const msg = $("serverMsg");
  msg.textContent = "測試連線中……";
  const r = await fetch("/api/settings/test", { method: "POST" });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  msg.textContent = data.ok
    ? "連線成功，金鑰能用。"
    : "連線失敗：" + (data.error || "請檢查金鑰是否正確。");
});

// ---- 設定：伺服器（多 Kaggle 帳號）----
async function loadServers() {
  try {
    const r = await fetch("/api/servers");
    if (r.status === 401) { showLogin(); return; }
    const data = await r.json();
    servers = data.servers || [];
    currentServerId = data.current_server_id || null;
    const sel = $("serverSelect");
    sel.innerHTML = "";
    if (!servers.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "尚未設定";
      sel.appendChild(opt);
      sel.disabled = true;
    } else {
      sel.disabled = false;
      servers.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = s.name;
        if (s.id === currentServerId) opt.selected = true;
        sel.appendChild(opt);
      });
    }
    renderServerList();
    renderHistoryFilter();
  } catch (e) {
    // 暫時連不上就等下一次刷新。
  }
}

function renderServerList() {
  const box = $("serverList");
  box.innerHTML = "";
  if (!servers.length) {
    box.innerHTML = '<p class="note">還沒有伺服器，在下面新增一個。</p>';
    return;
  }
  servers.forEach((s) => {
    const row = document.createElement("div");
    row.className = "server-row";
    const isCurrent = s.id === currentServerId;
    row.innerHTML = `
      <div class="server-meta">
        <p class="server-name">${escapeHtml(s.name)}${isCurrent ? ' <span class="server-current">目前</span>' : ""}</p>
        <p class="note">帳號：${escapeHtml(maskName(s.username))}　金鑰：${s.key_saved ? "已保存" + (s.key_hint || "") : "未填"}</p>
        <p class="note">數據集：${s.dataset ? escapeHtml(maskDataset(s.dataset)) : "未配（每次下載模型）"}</p>
      </div>`;
    const actions = document.createElement("div");
    actions.className = "server-actions";
    if (!isCurrent) {
      const act = document.createElement("button");
      act.type = "button";
      act.textContent = "設為目前";
      act.addEventListener("click", () => activateServer(s.id));
      actions.appendChild(act);
    }
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "編輯";
    edit.addEventListener("click", () => startServerEdit(s));
    actions.appendChild(edit);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn-danger";
    del.textContent = "刪除";
    del.addEventListener("click", () => deleteServer(s));
    actions.appendChild(del);
    row.appendChild(actions);
    box.appendChild(row);
  });
}

async function activateServer(id) {
  const r = await fetch(`/api/servers/${encodeURIComponent(id)}/activate`, { method: "POST" });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (!r.ok) {
    $("serverMsg").textContent = "切換失敗：" + (data.error || "請稍後再試。");
    await loadServers();
    return;
  }
  $("serverMsg").textContent = `已切換到「${data.server.name}」，之後的新任務都用它跑。`;
  currentServerId = data.server.id;
  // 切換伺服器後，歷史篩選跟著切到新伺服器，狀態數字也跟著換。
  historyServerFilter = "current";
  await loadSettings();
  await loadServers();
  await loadQuota();
  await loadStatus();
  await refreshJobs();
}

function startServerEdit(s) {
  editingServerId = s.id;
  $("serverFormTitle").textContent = "編輯伺服器";
  $("serverName").value = s.name;
  $("serverUser").value = "";
  $("serverUser").placeholder = `已保存：${maskName(s.username)}（留空表示不改）`;
  $("serverKey").value = "";
  $("serverKey").placeholder = s.key_saved ? "已保存的金鑰（留空表示不改）" : "貼上 kaggle.json 裡的 key";
  $("serverDataset").value = "";
  $("serverDataset").placeholder = s.dataset
    ? `已保存：${maskDataset(s.dataset)}（留空表示不改）`
    : "帳號名/數據集名";
  $("cancelServerEditBtn").classList.remove("hidden");
  $("serverMsg").textContent = "";
  $("serverName").focus();
}

function resetServerForm() {
  editingServerId = null;
  $("serverFormTitle").textContent = "新增伺服器";
  $("serverName").value = "";
  $("serverUser").value = "";
  $("serverUser").placeholder = "請輸入 Kaggle 用戶名";
  $("serverKey").value = "";
  $("serverKey").placeholder = "貼上 kaggle.json 裡的 key";
  $("serverDataset").value = "";
  $("serverDataset").placeholder = "帳號名/數據集名";
  $("cancelServerEditBtn").classList.add("hidden");
}

$("cancelServerEditBtn").addEventListener("click", () => {
  resetServerForm();
  $("serverMsg").textContent = "";
});

$("saveServerBtn").addEventListener("click", async () => {
  const name = $("serverName").value.trim();
  const username = $("serverUser").value.trim();
  const key = $("serverKey").value.trim();
  const dataset = $("serverDataset").value.trim();
  const msg = $("serverMsg");
  if (!name) {
    msg.textContent = "請給伺服器起個名字。";
    return;
  }
  // 新增時用戶名必填；編輯時留空表示沿用原值。
  if (!editingServerId && !username) {
    msg.textContent = "請填 Kaggle 用戶名。";
    return;
  }
  if (!editingServerId && !key) {
    msg.textContent = "新增伺服器要填金鑰。";
    return;
  }
  msg.textContent = "保存中……";
  const body = { name, username, key };
  // 編輯時數據集留空表示沿用原值：不要把 dataset 放進 body，
  // 後端收到空字串會把數據集清空。新增時照常帶上（可留空）。
  if (!editingServerId || dataset) body.dataset = dataset;
  const url = editingServerId
    ? `/api/servers/${encodeURIComponent(editingServerId)}`
    : "/api/servers";
  const r = await fetch(url, {
    method: editingServerId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (!r.ok) {
    msg.textContent = "保存失敗：" + (data.error || "請稍後再試。");
    return;
  }
  msg.textContent = editingServerId ? "已保存。" : `已新增伺服器「${data.server.name}」。`;
  resetServerForm();
  await loadServers();
  await loadSettings();
  await loadQuota();
  await loadStatus();
});

async function deleteServer(s) {
  if (!window.confirm(`確定刪除伺服器「${s.name}」嗎？這個帳號以後就不能用來生圖了，已經跑過的記錄不受影響。`)) return;
  const msg = $("serverMsg");
  const r = await fetch(`/api/servers/${encodeURIComponent(s.id)}`, { method: "DELETE" });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (!r.ok) {
    msg.textContent = "刪除失敗：" + (data.error || "請稍後再試。");
    return;
  }
  if (editingServerId === s.id) resetServerForm();
  msg.textContent = `已刪除伺服器「${s.name}」。`;
  await loadServers();
  await loadSettings();
  await loadQuota();
  await loadStatus();
}

$("serverSelect").addEventListener("change", () => {
  const id = $("serverSelect").value;
  if (id) activateServer(id);
});

// ---- 圖片模糊：每張圖各自帶開關 ----
$("blurToggleBtn").addEventListener("click", () => {
  if (displayedJob) toggleReveal(displayedJob.id);
});
$("resultImgWrap").addEventListener("click", () => {
  if (displayedJob) toggleReveal(displayedJob.id);
});

// ---- 歷史記錄：按伺服器篩選 ----
$("historyServerFilter").addEventListener("change", () => {
  historyServerFilter = $("historyServerFilter").value;
  refreshJobs();
});

// ---- 設定：修改密碼 ----
$("logoutBtn").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  showLogin();
});

$("changePassBtn").addEventListener("click", async () => {
  const current = $("currentPass").value;
  const next = $("newPass").value;
  const confirm = $("confirmPass").value;
  const msg = $("securityMsg");
  if (!current || !next || !confirm) {
    msg.textContent = "三個密碼框都要填。";
    return;
  }
  if (next !== confirm) {
    msg.textContent = "兩次新密碼輸入的不一樣，請重新輸入。";
    return;
  }
  msg.textContent = "保存中……";
  const r = await fetch("/api/auth/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: current, new_password: next }),
  });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (r.ok && data.ok) {
    $("currentPass").value = "";
    $("newPass").value = "";
    $("confirmPass").value = "";
    msg.textContent = "密碼已改好，請用新密碼重新登入。";
    showLogin();
  } else {
    msg.textContent = "改密碼失敗：" + (data.error || "請稍後再試。");
  }
});

// ---- 伺服器狀態 ----
async function loadStatus() {
  try {
    const r = await fetch("/api/status");
    if (r.status === 401) { showLogin(); return; }
    const s = await r.json();
    const kaggle = !s.kaggle_configured
      ? "Kaggle：尚未設定金鑰"
      : s.kaggle_reachable
        ? "Kaggle：已連線"
        : `Kaggle：連不上（${s.kaggle_error || "原因不明"}）`;
    $("serverStatusLine").textContent =
      `已運行 ${fmtUptime(s.uptime_seconds)}　${kaggle}　` +
      `額度剩 ${fmtSeconds(s.quota.remaining_seconds)}`;
    const cur = s.current_job;
    $("serverStatusCurrent").textContent =
      (cur
        ? `目前任務：${truncateText(cur.prompt, 40)}`
        : "目前沒有進行中的任務。") +
      `　排隊 ${s.counts.queued}　進行中 ${s.counts.running}`;
  } catch (e) {
    $("serverStatusLine").textContent = "伺服器狀態讀取失敗，請稍後再試。";
    $("serverStatusCurrent").textContent = "";
  }
}

// ---- 生圖：模式與參數 ----
const ASPECT_SIZES = {
  "1:1": [768, 768],
  "16:9": [1024, 576],
  "9:16": [576, 1024],
  "4:3": [1024, 768],
  "3:4": [768, 1024],
  "3:2": [1024, 672],
  "2:3": [672, 1024],
};
const MODE_HELP = {
  t2i: "只用文字描述畫面，不用上傳圖片。適合從零開始生一張新圖。",
  single: "上傳 1 張參考圖，再寫一句話告訴模型要怎麼改它。在要求裡用 <image1> 指這張圖，例如：把 <image1> 裡的人放到海灘上。",
  multi: "上傳 2 到 4 張參考圖，再寫一句話告訴模型怎麼組合它們。在要求裡用 <image1>、<image2> 指第幾張圖，例如：把 <image1> 裡的人放到 <image2> 的海灘上。",
};
const REF_HELP = {
  single: "只選 1 張圖（jpg、png、webp，每張不超過 10MB）。這張圖就是場景／畫布，會按你選的尺寸先縮好。",
  multi: "最多選 4 張圖（jpg、png、webp，每張不超過 10MB）。第 1 張是場景／畫布（要被編輯的那張圖），第 2 張起是提供內容的參考圖（例如人物、衣服）。",
};
const MODE_LABEL = { t2i: "純文字生圖", single: "單圖參考", multi: "多圖參考" };
let genMode = "t2i";
let refFiles = [];
// 結構化提示詞框的 dirty 標誌：使用者手動改過就不再自動跟隨初稿；
// 擴寫生成、提交、生圖後都清掉（視為系統生成、尚未手改）。
let structuredDirty = false;

function fmtOptions(opts) {
  if (!opts) return "";
  const parts = [MODE_LABEL[opts.mode] || "純文字生圖"];
  if (opts.mode && opts.mode !== "t2i") parts.push(`${opts.ref_count || 0} 張參考`);
  if (opts.aspect) parts.push(opts.aspect);
  if (opts.seed !== undefined && opts.seed !== null) parts.push(`種子 ${opts.seed}`);
  return parts.join(" · ");
}

function fmtOptionsFull(opts) {
  if (!opts) return "";
  const parts = [MODE_LABEL[opts.mode] || "純文字生圖"];
  if (opts.mode && opts.mode !== "t2i") {
    parts.push(`${opts.ref_count || 0} 張參考`);
  }
  if (opts.aspect) parts.push(opts.aspect);
  if (opts.width && opts.height) parts.push(`${opts.width}x${opts.height}`);
  if (opts.seed !== undefined && opts.seed !== null) parts.push(`種子 ${opts.seed}`);
  if (opts.steps) parts.push(`${opts.steps} 步`);
  if (opts.cfg !== undefined && opts.cfg !== null) parts.push(`CFG ${opts.cfg}`);
  if (opts.negative_prompt) parts.push(`負面 ${truncateText(opts.negative_prompt, 40)}`);
  return parts.join(" · ");
}

function setMode(mode) {
  genMode = mode;
  document.querySelectorAll("#modeSeg .mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  $("modeHelp").textContent = MODE_HELP[mode];
  const isRef = mode !== "t2i";
  $("refArea").classList.toggle("hidden", !isRef);
  if (isRef) {
    $("refHelp").textContent = REF_HELP[mode];
    $("refInput").multiple = mode === "multi";
    if (mode === "single" && refFiles.length > 1) {
      refFiles = refFiles.slice(0, 1);
    }
  } else {
    refFiles = [];
  }
  $("promptLabel").textContent = isRef
    ? "口語初稿（寫清楚要怎麼改參考圖）"
    : "口語初稿（中文就可以，直接寫你想要的畫面）";
  $("prompt").placeholder = isRef
    ? "例如：把 <image1> 裡的人放到海灘上，換成傍晚的陽光"
    : "例如：白天的小電腦店門面，招牌寫著 CHUFENG，櫥窗裡有一台桌上型電腦";
  renderRefThumbs();
}

document.querySelectorAll("#modeSeg .mode-btn").forEach((b) => {
  b.addEventListener("click", () => setMode(b.dataset.mode));
});

function renderRefThumbs() {
  const box = $("refThumbs");
  box.innerHTML = "";
  const max = genMode === "multi" ? 4 : 1;
  $("refCount").textContent =
    refFiles.length ? `已選 ${refFiles.length} 張（最多 ${max} 張）` : "";
  refFiles.forEach((file, i) => {
    const wrap = document.createElement("div");
    wrap.className = "ref-thumb";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.alt = file.name;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "×";
    btn.title = "移除這張圖";
    btn.addEventListener("click", () => {
      refFiles.splice(i, 1);
      renderRefThumbs();
    });
    wrap.appendChild(img);
    wrap.appendChild(btn);
    box.appendChild(wrap);
  });
}

$("refInput").addEventListener("change", () => {
  const picked = Array.from($("refInput").files || []);
  const msg = $("generateMsg");
  const max = genMode === "multi" ? 4 : 1;
  const okTypes = ["image/jpeg", "image/png", "image/webp"];
  const okExts = [".jpg", ".jpeg", ".png", ".webp"];
  for (const f of picked) {
    const ext = f.name.slice(f.name.lastIndexOf(".")).toLowerCase();
    if (!okTypes.includes(f.type) && !okExts.includes(ext)) {
      msg.textContent = `「${f.name}」格式不對，只接受 jpg、png、webp。`;
      $("refInput").value = "";
      return;
    }
    if (f.size > 10 * 1024 * 1024) {
      msg.textContent = `「${f.name}」超過 10MB，請換張小一點的圖。`;
      $("refInput").value = "";
      return;
    }
  }
  if (genMode === "single") {
    refFiles = picked.slice(0, 1);
  } else {
    refFiles = refFiles.concat(picked).slice(0, max);
  }
  if (picked.length && refFiles.length >= max && picked.length > max) {
    msg.textContent = `多圖參考最多 ${max} 張，只留了前 ${max} 張。`;
  } else {
    msg.textContent = "";
  }
  $("refInput").value = "";
  renderRefThumbs();
});

$("aspect").addEventListener("change", () => {
  const size = ASPECT_SIZES[$("aspect").value];
  if (size) {
    $("width").value = size[0];
    $("height").value = size[1];
  }
});

$("randomSeedBtn").addEventListener("click", () => {
  $("seed").value = String(Math.floor(Math.random() * 1e15));
});

function collectOptions() {
  return {
    mode: genMode,
    aspect: $("aspect").value,
    width: $("width").value,
    height: $("height").value,
    seed: $("seed").value.trim(),
    steps: $("steps").value,
    cfg: $("cfg").value,
    negative_prompt: $("negativePrompt").value.trim(),
  };
}

// ---- 導演級擴寫（Meta-Prompt）：純前端規則，不用 API、不花額度 ----
// 把「圖片N」「图N」「imageN」寫法統一轉成 <imageN>；已是 <imageN> 的原樣保留。
function translateImageRefs(text) {
  const kept = [];
  let out = String(text || "").replace(/<image([1-4])>/gi, (m) => {
    kept.push(m.toLowerCase());
    return "\u0000" + (kept.length - 1) + "\u0000";
  });
  out = out.replace(/(?:圖片|图片)\s*([1-4])|(?:圖|图)\s*([1-4])|image\s*([1-4])/gi,
    (m, a, b, c) => "<image" + (a || b || c) + ">");
  out = out.replace(/\u0000(\d+)\u0000/g, (m, i) => kept[Number(i)]);
  return out;
}

// 關鍵詞對照表（按順序套用，先配到的先吃掉原文，長詞組優先於單字）。
// 桶：subject 主體、scene 場景、action 動作、light 光線、camera 鏡頭、style 風格。
// 詞庫來源：Awesome-muse-image-prompts-and-skills（data/prompts.json 13 條）
// 的高頻寫法——人像保真（id 2/8/11）、光線術語（id 2）、鏡頭規格（id 8/11/12）、
// 質感詞（id 4/8/11）、角色分工（id 13），再按電商服飾場景補齊。
const META_RULES = [
  [/電腦店|电脑店/, "computer shop", "subject"],
  [/門面|门面|店面|店門|店门/, "storefront", "subject"],
  [/招牌/, "store sign", "subject"],
  [/櫃台|櫃檯|柜台/, "glass counter", "subject"],
  [/桌上型電腦|台式電腦|桌上电脑/, "desktop computer", "subject"],
  [/筆電|筆記型電腦|笔记本电脑/, "laptop", "subject"],
  [/電腦|电脑/, "desktop computer", "subject"],
  [/螢幕|屏幕/, "screen", "subject"],
  [/鍵盤|键盘/, "keyboard", "subject"],
  [/手機|手机/, "smartphone", "subject"],
  [/女子|女人|女生|女孩/, "woman", "subject"],
  [/男子|男人|男生|男孩/, "man", "subject"],
  [/帥哥|帅哥/, "handsome man", "subject"],
  [/美女/, "beautiful woman", "subject"],
  [/妹子/, "young woman", "subject"],
  [/人像|肖像/, "portrait", "subject"],
  [/人/, "person", "subject"],
  [/中國風|中国风/, "Chinese style", "subject"],
  [/中國|中国/, "Chinese", "subject"],
  [/日本/, "Japanese", "subject"],
  [/貓|猫/, "cat", "subject"],
  [/狗/, "dog", "subject"],
  [/花/, "flowers", "subject"],
  [/樹|树/, "tree", "subject"],
  [/車|车/, "car", "subject"],
  [/咖啡廳|咖啡店|咖啡馆/, "cozy café", "scene"],
  [/獻酒|献酒|敬酒/, "offering wine", "subject"],
  [/酒/, "wine", "subject"],
  [/咖啡/, "coffee", "subject"],
  [/書|书/, "book", "subject"],
  [/桌子/, "table", "subject"],
  [/椅子/, "chair", "subject"],
  [/窗/, "window", "subject"],
  [/門|门/, "door", "subject"],
  [/霓虹燈|霓虹灯/, "neon lights", "light"],
  [/燈光|灯光/, "warm practical lights", "light"],
  [/燈|灯/, "lamp", "subject"],
  [/裙子/, "dress", "subject"],
  [/比基尼/, "bikini", "subject"],
  [/眼鏡|眼镜/, "glasses", "subject"],
  [/模特|模特兒/, "professional fashion model", "subject"],
  [/穿搭/, "styled outfit", "subject"],
  [/版型/, "tailored fit", "subject"],
  [/修身/, "slim fit", "subject"],
  [/面料/, "premium fabric", "subject"],
  [/連衣裙|连衣裙|洋裝|洋装/, "elegant dress", "subject"],
  [/旗袍/, "cheongsam", "subject"],
  [/漢服|汉服/, "hanfu traditional dress", "subject"],
  [/西裝|西装/, "tailored suit", "subject"],
  [/婚紗|婚纱/, "wedding gown", "subject"],
  [/絲綢|丝绸/, "silk fabric", "subject"],
  [/牛仔/, "denim", "subject"],
  [/皮衣/, "leather jacket", "subject"],
  [/珠寶|珠宝/, "jewelry", "subject"],
  [/手錶|手表/, "luxury watch", "subject"],
  [/高跟鞋/, "high heels", "subject"],
  [/外套|大衣/, "coat", "subject"],
  [/氣質|气质/, "elegant demeanor", "subject"],
  [/帽子/, "hat", "subject"],
  [/紅色|红色/, "red", "subject"],
  [/藍色|蓝色/, "blue", "subject"],
  [/綠色|绿色/, "green", "subject"],
  [/黃色|黄色/, "yellow", "subject"],
  [/白色/, "white", "subject"],
  [/黑色/, "black", "subject"],
  [/金色/, "golden", "subject"],
  [/銀色|银色/, "silver", "subject"],
  [/粉色/, "pink", "subject"],
  [/紫色/, "purple", "subject"],
  [/可愛|可爱/, "cute", "subject"],
  [/漂亮/, "beautiful", "subject"],
  [/復古|复古/, "vintage", "subject"],
  [/現代|现代/, "modern", "subject"],
  [/科幻/, "sci-fi", "subject"],
  [/未來|未来/, "futuristic", "subject"],
  [/溫馨|温馨/, "cozy", "subject"],
  [/浪漫/, "romantic", "subject"],
  [/夢幻|梦幻/, "dreamy", "subject"],
  [/街道|馬路|马路|街頭|街头/, "street", "scene"],
  [/海灘|沙灘|海滩/, "beach", "scene"],
  [/辦公室|办公室/, "office", "scene"],
  [/臥室|卧室/, "bedroom", "scene"],
  [/影棚/, "photo studio", "scene"],
  [/白底/, "clean white background", "scene"],
  [/圖書館|图书馆/, "library", "scene"],
  [/公園|公园/, "park", "scene"],
  [/森林/, "forest", "scene"],
  [/湖泊/, "lake", "scene"],
  [/湖/, "lake", "scene"],
  [/城市/, "cityscape", "scene"],
  [/夜景/, "night cityscape", "scene"],
  [/商場|商场|專櫃|专柜/, "boutique store", "scene"],
  [/廚房|厨房/, "kitchen", "scene"],
  [/陽台|阳台/, "balcony", "scene"],
  [/海邊|海边/, "seaside", "scene"],
  [/山/, "mountains", "scene"],
  [/海/, "sea", "scene"],
  [/天空/, "sky", "scene"],
  [/雲|云/, "clouds", "scene"],
  [/店內|店里|店裡/, "inside the shop", "scene"],
  [/場景|场景/, "the scene", "scene"],
  [/站著|站在|站/, "standing", "action"],
  [/坐著|坐在|坐/, "sitting", "action"],
  [/大笑/, "laughing", "action"],
  [/沉思/, "thoughtful expression", "action"],
  [/自信/, "confident expression", "action"],
  [/回眸/, "looking back over the shoulder", "action"],
  [/凝視|凝视/, "gazing", "action"],
  [/伸手/, "reaching out", "action"],
  [/微笑|笑/, "smiling", "action"],
  [/走路|走/, "walking", "action"],
  [/看著|看着|看/, "looking", "action"],
  [/拿著|拿着|拿/, "holding", "action"],
  [/恭敬/, "respectfully", "action"],
  [/合成/, "seamlessly composited", "action"],
  [/放到|放在/, "placed in", "action"],
  [/陽光下|阳光下/, "sunlit", "light"],
  [/上午/, "morning light", "light"],
  [/傍晚|黃昏|黄昏/, "dusk light", "light"],
  [/下午|午後|午后/, "warm afternoon light", "light"],
  [/晚上|夜晚|夜間|夜间/, "night", "light"],
  [/霓虹/, "neon lights", "light"],
  [/陽光|阳光|日光/, "sunlight", "light"],
  [/黃金時刻|黄金时刻|金色時刻|金色时刻/, "golden hour", "light"],
  [/逆光/, "backlit with rim light", "light"],
  [/輪廓光|轮廓光/, "rim light", "light"],
  [/日出/, "sunrise", "light"],
  [/柔光/, "soft diffused light", "light"],
  [/自然光/, "natural daylight", "light"],
  [/月光/, "moonlight", "light"],
  [/燭光|烛光/, "candlelight", "light"],
  [/體積光|体积光/, "volumetric light", "light"],
  [/柔和/, "soft light", "light"],
  [/雨/, "rainy", "light"],
  // 鏡頭段（倉庫 id 8/11/12 的鏡頭規格寫法）。
  [/特寫|特写|近景/, "close-up portrait", "camera"],
  [/全身/, "full-body portrait", "camera"],
  [/半身/, "half-body portrait", "camera"],
  [/淺景深|浅景深/, "shallow depth of field", "camera"],
  [/廣角|广角/, "wide-angle lens", "camera"],
  [/俯視|俯视|鳥瞰|鸟瞰/, "aerial view", "camera"],
  [/仰拍/, "low-angle shot", "camera"],
  [/側臉|侧脸/, "side profile", "camera"],
  // 風格質感段（倉庫 id 4/8/11 的質感與風格寫法）。
  [/電影感|电影感|電影|电影/, "cinematic", "style"],
  [/紀實|纪实/, "documentary style", "style"],
  [/寫實|写实|超寫實|超写实/, "ultra-realistic", "style"],
  [/插畫|插画/, "illustration style", "style"],
  [/水彩/, "watercolor style", "style"],
  [/膠片感|胶片感/, "subtle film grain", "style"],
  [/雜誌風|杂志风/, "editorial magazine style", "style"],
  [/商業廣告|商业广告/, "commercial advertising photography", "style"],
  [/皮膚質感|皮肤质感/, "natural skin texture", "style"],
  [/髮絲|发丝/, "detailed hair texture", "style"],
  [/布料質感|布料质感/, "fabric texture", "style"],
  [/冷色調|冷色调/, "cool color grade", "style"],
  [/暖色調|暖色调/, "warm color grade", "style"],
  [/霧面|雾面/, "matte finish", "style"],
];

// 把口語初稿擴寫成結構化英文提示詞，按倉庫高品質提示詞的六段結構
// 固定順序：主體與動作 → 場景環境 → 光線 → 鏡頭與構圖 → 風格質感 → 質量詞。
// <imageN> 引用與 CHUFENG 這類專名原樣保留；對不上的中文原樣留著
// （Qwen 編碼器本身認中文），不丟資訊。
function expandMetaPrompt(draft, mode) {
  const text = translateImageRefs(draft);
  const refs = [...new Set(text.match(/<image[1-4]>/gi) || [])]
    .map((r) => r.toLowerCase());
  let work = text.replace(/<image[1-4]>/gi, " ");
  const subject = [], scene = [], action = [], light = [], camera = [], style = [];
  const pushTo = (bucket, en) => {
    const arr = bucket === "subject" ? subject
      : bucket === "scene" ? scene
      : bucket === "action" ? action
      : bucket === "camera" ? camera
      : bucket === "style" ? style : light;
    if (en && !arr.includes(en)) arr.push(en);
  };
  // 招牌寫著 CHUFENG → sign reading CHUFENG（專名原樣保留）。
  work = work.replace(/招牌[^，,。.!！?？\n]{0,20}?([A-Za-z][A-Za-z0-9_-]*)/g, (m, t) => {
    pushTo("subject", "sign reading " + t);
    return " ";
  });
  // 組合片語（先處理，避免被單字規則拆開）。
  work = work.replace(/(電腦店|电脑店)\s*(門面|门面|店面|店門|店门)/, (m) => {
    pushTo("subject", "computer shop storefront");
    return " ";
  });
  work = work.replace(/(發光|发光)\s*(電腦|电脑)/, (m) => {
    pushTo("subject", "glowing desktop computer");
    return " ";
  });
  work = work.replace(/(發光|发光)\s*(螢幕|屏幕)/, (m) => {
    pushTo("subject", "glowing screen");
    return " ";
  });
  work = work.replace(/(櫃台|櫃檯|柜台)[^，,。.!！?？\n]{0,10}?(裡|里)/, (m) => {
    pushTo("scene", "inside the glass counter");
    return " ";
  });
  // 單字對照：按表順序套用，配到的就吃掉原文（同一詞出現多次一次吃乾淨，
  // 免得重複的詞留在殘詞裡）。
  for (const [re, en, bucket] of META_RULES) {
    if (re.test(work)) {
      pushTo(bucket, en);
      work = work.replace(new RegExp(re.source, "g"), " ");
    }
  }
  // 剩下的拉丁字母專名（CHUFENG、RTX 5090 這類）原樣保留。
  const latin = work.match(/[A-Za-z][A-Za-z0-9 _-]{1,}/g) || [];
  for (const t of [...new Set(latin.map((s) => s.trim()))]) {
    if (!t) continue;
    pushTo("subject", t);
    work = work.replace(t, " ");
  }
  // 剩下的中文：去掉量詞、虛詞與標點，留下的原樣保留，不丟資訊。
  // 單字殘片（穿、時、直這類）幾乎不帶資訊，丟掉；多字殘詞留著給模型讀。
  // 「明顯／形象」這類修飾語的意思已由翻譯出的詞表達，殘詞裡直接去掉。
  let rest = work
    .replace(/一[台隻只張张座條条個个]/g, " ")
    .replace(/[有的是在了和與与跟把被很非常要讓让請请到里上中着一正]/g, " ")
    .replace(/明顯|明显|形象/g, " ")
    .replace(/[，,。.!！?？；;：:、（）()\s]+/g, " ")
    .trim();
  rest = rest.split(" ").filter((t) => t.length >= 2).join(" ");
  if (rest.replace(/\s/g, "").length < 2) rest = "";
  // 發光主體＋櫃台內位置合併成一句。
  const glowIdx = subject.findIndex((s) => s.startsWith("glowing "));
  const inIdx = scene.findIndex((s) => s.startsWith("inside "));
  if (glowIdx >= 0 && inIdx >= 0) {
    const merged = subject[glowIdx] + " " + scene[inIdx];
    subject.splice(glowIdx, 1);
    scene.splice(inIdx, 1);
    subject.push(merged);
  }
  const parts = [];
  if (refs.length) parts.push(refs.join(" "));
  // 參考模式：先定調「保住原圖」再寫新內容（倉庫 id 2 的寫法）。
  const isRef = mode && mode !== "t2i";
  if (isRef) parts.push("preserve the original composition, lighting, and identity");
  parts.push(...subject, ...action);
  parts.push(...scene);
  if (light.length) parts.push(...light);
  else parts.push("soft natural light");
  parts.push("cinematic lighting");
  // 鏡頭段：景別 → 構圖 → 人像加 85mm（倉庫 id 8、11 的寫法）。
  const cameraParts = [...camera];
  const hasStorefront = subject.some((s) => s.includes("storefront"));
  cameraParts.push(hasStorefront ? "eye-level storefront composition" : "eye-level composition");
  const hasPerson = subject.some((s) => /woman|man|portrait/.test(s));
  if (hasPerson) {
    cameraParts.push("shot on a full-frame camera, 85mm lens");
    if (!camera.includes("shallow depth of field")) cameraParts.push("shallow depth of field");
  }
  parts.push(...cameraParts);
  // 風格質感段：沒寫風格就預設寫實。
  if (style.length) parts.push(...style);
  else parts.push("photorealistic");
  parts.push("cinematic color grading");
  parts.push(isRef
    ? "ultra-detailed, seamless edit, 8k quality, masterpiece quality"
    : "ultra-detailed, 8k quality, masterpiece quality");
  if (rest) parts.push(rest);
  return parts.filter(Boolean).join(", ");
}

// 負面提示詞預設（倉庫 id 8「No facial drift, body distortion, extra limbs,
// text, watermark…」的寫法）：畫面有招牌文字時不加 text，免得跟正面提示詞打架。
const DEFAULT_NEGATIVE = "blurry, low quality, distorted, deformed hands, extra limbs, bad anatomy, watermark";
function defaultNegative(draft) {
  const hasText = /招牌|文字|標題|标题|标语|標語|text|sign|letter/i.test(draft || "");
  return hasText ? DEFAULT_NEGATIVE : DEFAULT_NEGATIVE + ", text";
}

$("expandBtn").addEventListener("click", () => {
  const draft = $("prompt").value.trim();
  const msg = $("expandMsg");
  if (!draft) {
    msg.textContent = "先寫一句初稿再擴寫。";
    return;
  }
  $("structuredPrompt").value = expandMetaPrompt(draft, genMode);
  structuredDirty = false;
  // 負面框空著才自動填預設（使用者自己改過的不要蓋掉）。
  if (!$("negativePrompt").value.trim()) {
    $("negativePrompt").value = defaultNegative(draft);
  }
  msg.textContent = "已擴寫，可以再改，改完直接按「開始生圖」。";
});

// 初稿與結構化框同步：沒手改過結構化框時，初稿改動就自動跟隨。
$("prompt").addEventListener("input", () => {
  if (!structuredDirty) $("structuredPrompt").value = $("prompt").value;
});
$("structuredPrompt").addEventListener("input", () => {
  structuredDirty = true;
});

// ---- 生圖 ----
$("generateBtn").addEventListener("click", async () => {
  // 提交的提示詞：結構化框有內容就用它，否則用初稿。
  const draft = $("prompt").value.trim();
  const structured = $("structuredPrompt").value.trim();
  const prompt = structured || draft;
  const msg = $("generateMsg");
  if (!configured) {
    $("keyWarning").classList.remove("hidden");
    msg.textContent = "請先去「設定」頁填寫 Kaggle 金鑰。";
    showTab("settings");
    return;
  }
  if (!prompt) {
    msg.textContent = "請先輸入生圖要求。";
    return;
  }
  const options = collectOptions();
  let r;
  msg.textContent = "提交中……";
  if (genMode === "t2i") {
    r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, ...options }),
    });
  } else {
    if (genMode === "single" && refFiles.length !== 1) {
      msg.textContent = "單圖參考要上傳 1 張圖。";
      return;
    }
    if (genMode === "multi" && refFiles.length < 1) {
      msg.textContent = "多圖參考至少要上傳 1 張圖，最多 4 張。";
      return;
    }
    const form = new FormData();
    form.append("prompt", prompt);
    form.append("mode", genMode);
    form.append("aspect", options.aspect);
    form.append("width", options.width);
    form.append("height", options.height);
    form.append("seed", options.seed);
    form.append("steps", options.steps);
    form.append("cfg", options.cfg);
    form.append("negative_prompt", options.negative_prompt);
    refFiles.forEach((f) => form.append("refs", f, f.name));
    r = await fetch("/api/jobs/with-refs", { method: "POST", body: form });
  }
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (!r.ok) {
    msg.textContent = "提交失敗：" + (data.error || "請稍後再試。");
    return;
  }
  currentJobId = data.id;
  previewJob = null;
  msg.textContent = "已排隊，一張約 4 到 5 分鐘，頁面會自動更新狀態。";
  $("prompt").value = "";
  // 結構化框不清空（留著給使用者複用），dirty 清掉，之後的新初稿可再同步。
  structuredDirty = false;
  refFiles = [];
  renderRefThumbs();
  await refreshJobs();
  await loadStatus();
});

function renderCurrentJob() {
  const box = $("currentJob");
  const autoJob = jobs.find((j) => j.id === currentJobId) || jobs[0];
  // 使用者在歷史記錄點了一張圖，就顯示那張；否則顯示最新一張任務。
  // 每次刷新都按 id 重新對上最新資料，狀態變了預覽也會跟著變；被刪掉就回到自動。
  if (previewJob) {
    previewJob = jobs.find((j) => j.id === previewJob.id) || null;
  }
  const job = previewJob || autoJob;
  displayedJob = job || null;
  if (!job) {
    box.classList.add("hidden");
    $("previewEmpty").classList.remove("hidden");
    return;
  }
  $("previewEmpty").classList.add("hidden");
  if (!previewJob) currentJobId = job.id;
  box.classList.remove("hidden");
  $("currentStatus").textContent =
    `狀態：${STATUS_TEXT[job.status] || job.status}　建立時間：${fmtTime(job.created_at)}` +
    (job.finished_at ? `　完成時間：${fmtTime(job.finished_at)}` : "") +
    (job.server ? `　伺服器：${job.server}` : "");
  $("currentPrompt").textContent = job.prompt;
  $("currentOptions").textContent = fmtOptionsFull(job.options);
  // 預覽區底部快照參數標籤：模式 · 比例 · 尺寸 · 種子 · 步數 · CFG。
  // 種子用後端存的 options.seed（已是實際用的種子，留空時後端會隨機產生再存回）。
  const tags = $("snapshotTags");
  if (tags) {
    const o = job.options || {};
    const tagParts = [MODE_LABEL[o.mode] || "純文字生圖"];
    if (o.aspect) tagParts.push(o.aspect);
    if (o.width && o.height) tagParts.push(o.width + "×" + o.height);
    const seedVal = (o.seed !== undefined && o.seed !== null && String(o.seed) !== "")
      ? o.seed : null;
    tagParts.push("種子 " + (seedVal !== null ? seedVal : "隨機"));
    if (o.steps) tagParts.push(o.steps + " 步");
    if (o.cfg !== undefined && o.cfg !== null) tagParts.push("CFG " + o.cfg);
    tags.textContent = tagParts.join(" · ");
  }
  $("currentNote").textContent =
    job.status === "queued"
      ? "在排隊等前面的任務跑完，一次只跑一張。"
      : job.status === "running"
        ? (job.options && job.options.use_dataset
          ? "正在跑：用已掛載的數據集，不用重新下載模型，啟動 ComfyUI 生圖中，請耐心等幾分鐘。"
          : "正在跑：先下載 14.23GB 模型（這步最花時間），再啟動 ComfyUI 生圖，請耐心等幾分鐘。")
        : job.status === "completed"
          ? `已完成，生圖本身用了 ${fmtSeconds(job.generation_seconds)}，` +
            `整個任務用了 ${fmtSeconds(job.gpu_seconds)}。`
          : job.status === "cancelled"
            ? "已取消，不會再執行。"
            : "";
  const err = $("currentError");
  if (job.status === "failed") {
    err.textContent = "失敗原因：" + (job.error || "未知原因");
    err.classList.remove("hidden");
  } else {
    err.classList.add("hidden");
  }
  const img = $("resultImg");
  const wrap = $("resultImgWrap");
  const hint = $("resultBlurHint");
  const blurBtn = $("blurToggleBtn");
  const actions = $("resultActions");
  if (job.status === "completed") {
    img.src = `/api/jobs/${job.id}/image?t=${Date.now()}`;
    wrap.classList.remove("hidden");
    const blurred = applyImgBlur(img, job.id);
    hint.style.display = blurred ? "" : "none";
    blurBtn.textContent = blurred ? "👁 看原圖" : "🔒 模糊";
    const dl = $("downloadBtn");
    dl.href = `/api/jobs/${job.id}/image`;
    dl.setAttribute("download", `qwen-${job.id}.png`);
    actions.classList.remove("hidden");
  } else {
    wrap.classList.add("hidden");
    actions.classList.add("hidden");
  }
  // 預覽區的刪除按鈕對已結束的任務顯示（已完成、失敗、已取消）。
  $("detailDeleteBtn").style.display =
    ["completed", "failed", "cancelled"].includes(job.status) ? "" : "none";
  // 預覽區的取消按鈕只對排隊中或進行中的任務顯示。
  $("cancelJobBtn").style.display =
    job.status === "queued" || job.status === "running" ? "" : "none";
}

// ---- 歷史記錄 ----
function renderHistory() {
  const list = $("historyList");
  list.innerHTML = "";
  if (!jobs.length) {
    list.innerHTML = '<p class="note">還沒有任務，在「生圖」頁輸入要求開始第一張。</p>';
    return;
  }
  jobs.forEach((job) => {
    const card = document.createElement("div");
    card.className = "job-card";
    const blurred = job.status === "completed" && !revealedJobs.has(job.id);
    const thumb =
      job.status === "completed"
        ? `<span class="thumb-wrap"><img class="thumb${blurred ? " img-blurred" : ""}" src="/api/jobs/${job.id}/image" alt="縮圖" loading="lazy"><button type="button" class="thumb-eye" title="點一下看這張的原圖">${blurred ? "👁" : "🔒"}</button></span>`
        : '<div class="thumb none">無圖</div>';
    card.innerHTML = `
      ${thumb}
      <div class="job-info">
        <p class="prompt-label">原始提示詞</p>
        <p class="prompt">${escapeHtml(job.prompt)}</p>
        ${job.options ? `<p class="note">${escapeHtml(fmtOptions(job.options))}</p>` : ""}
        <p class="note">${STATUS_TEXT[job.status] || job.status}　${fmtTime(job.created_at)}${job.server ? "　伺服器：" + escapeHtml(job.server) : ""}</p>
        ${job.status === "failed" && job.error
          ? `<p class="error">失敗原因：${escapeHtml(job.error)}</p>` : ""}
      </div>`;
    const eye = card.querySelector(".thumb-eye");
    if (eye) {
      eye.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleReveal(job.id);
      });
    }
    card.addEventListener("click", () => showPreview(job));
    if (job.status === "completed" || job.status === "failed" ||
        job.status === "cancelled") {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "job-delete btn-danger";
      del.textContent = "刪除";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteJob(job);
      });
      card.appendChild(del);
    }
    if (job.status === "queued" || job.status === "running") {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "job-delete btn-danger";
      cancel.textContent = "取消";
      cancel.addEventListener("click", (e) => {
        e.stopPropagation();
        cancelJob(job);
      });
      card.appendChild(cancel);
    }
    list.appendChild(card);
  });
}

// ---- 歷史記錄：刪除 ----
async function deleteJob(job) {
  if (!job) return;
  if (!window.confirm("確定刪除這條記錄嗎？圖片也會一起刪掉。")) return;
  const msg = $("historyMsg");
  try {
    const r = await fetch(`/api/jobs/${encodeURIComponent(job.id)}`,
                          { method: "DELETE" });
    if (r.status === 401) { showLogin(); return; }
    const data = await r.json();
    if (!r.ok) {
      msg.textContent = "刪除失敗：" + (data.error || "請稍後再試。");
      return;
    }
    if (previewJob && previewJob.id === job.id) {
      previewJob = null;
    }
    if (currentJobId === job.id) currentJobId = null;
    msg.textContent = "已刪除。";
    await refreshJobs();
  } catch (e) {
    msg.textContent = "刪除失敗，請稍後再試。";
  }
}

// ---- 歷史記錄 / 預覽區：取消 ----
async function cancelJob(job) {
  if (!job) return;
  if (!window.confirm("確定要取消這個任務嗎？排隊中的任務取消後不會執行；進行中的任務取消後工作台不再等它（Kaggle 那邊可能還會跑一會才停）。")) return;
  const msg = $("historyMsg");
  try {
    const r = await fetch(`/api/jobs/${encodeURIComponent(job.id)}/cancel`,
                          { method: "POST" });
    if (r.status === 401) { showLogin(); return; }
    const data = await r.json();
    if (!r.ok) {
      msg.textContent = "取消失敗：" + (data.error || "請稍後再試。");
      return;
    }
    msg.textContent = "已取消。";
    await refreshJobs();
  } catch (e) {
    msg.textContent = "取消失敗，請稍後再試。";
  }
}

$("clearHistoryBtn").addEventListener("click", async () => {
  if (!window.confirm("確定清空全部已完成的歷史記錄嗎？進行中的任務不會動。")) return;
  const msg = $("historyMsg");
  msg.textContent = "清空中……";
  try {
    const r = await fetch("/api/jobs/clear", { method: "POST" });
    if (r.status === 401) { showLogin(); return; }
    const data = await r.json();
    if (!r.ok) {
      msg.textContent = "清空失敗：" + (data.error || "請稍後再試。");
      return;
    }
    previewJob = null;
    msg.textContent = `已清空 ${data.deleted || 0} 條記錄。`;
    await refreshJobs();
  } catch (e) {
    msg.textContent = "清空失敗，請稍後再試。";
  }
});

$("detailDeleteBtn").addEventListener("click", () => {
  if (displayedJob) deleteJob(displayedJob);
});

$("cancelJobBtn").addEventListener("click", () => {
  if (displayedJob) cancelJob(displayedJob);
});

function showPreview(job) {
  previewJob = job;
  renderCurrentJob();
  showTab("generate");
  $("currentJob").scrollIntoView({ behavior: "smooth" });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok;
  }
}

$("copyPromptBtn").addEventListener("click", async () => {
  if (!displayedJob) return;
  const ok = await copyText(displayedJob.prompt);
  $("copyMsg").textContent = ok ? "已複製提示詞。" : "複製失敗，請手動選取文字複製。";
});

$("reusePromptBtn").addEventListener("click", () => {
  if (!displayedJob) return;
  $("prompt").value = displayedJob.prompt;
  // 結構化框也一起帶回（視為系統填入、尚未手改，之後的新初稿可再同步）。
  $("structuredPrompt").value = displayedJob.prompt;
  structuredDirty = false;
  // 把上次的選項一起帶回面板（參考圖要重新上傳，瀏覽器留不住圖）。
  const opts = displayedJob.options || {};
  if (opts.mode) setMode(opts.mode);
  if (opts.aspect &&
      Array.from($("aspect").options).some((o) => o.value === opts.aspect)) {
    $("aspect").value = opts.aspect;
  }
  if (opts.width) $("width").value = opts.width;
  if (opts.height) $("height").value = opts.height;
  if (opts.seed !== undefined && opts.seed !== null) $("seed").value = opts.seed;
  if (opts.steps) $("steps").value = opts.steps;
  if (opts.cfg !== undefined && opts.cfg !== null) $("cfg").value = opts.cfg;
  if (opts.negative_prompt !== undefined && opts.negative_prompt !== null) {
    $("negativePrompt").value = opts.negative_prompt;
  }
  if (opts.mode && opts.mode !== "t2i") {
    $("generateMsg").textContent = "已帶回上次的設定，參考圖請重新上傳。";
  }
  $("prompt").focus();
  $("prompt").scrollIntoView({ behavior: "smooth", block: "center" });
});

// ---- 刷新 ----
function currentServerName() {
  const cur = servers.find((x) => x.id === currentServerId);
  return cur ? cur.name : null;
}

function historyFilterValue() {
  // "current" = 只看目前伺服器；"all" = 全部；其他 = 指定伺服器名字。
  if (historyServerFilter === "all") return "all";
  if (historyServerFilter === "current") return currentServerName() || "all";
  return historyServerFilter;
}

function renderHistoryFilter() {
  const sel = $("historyServerFilter");
  if (!sel) return;
  const cur = currentServerName();
  const prev = sel.value;
  sel.innerHTML = "";
  const optCur = document.createElement("option");
  optCur.value = "current";
  optCur.textContent = cur ? `目前伺服器（${cur}）` : "目前伺服器";
  sel.appendChild(optCur);
  servers.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.name;
    sel.appendChild(opt);
  });
  const optAll = document.createElement("option");
  optAll.value = "all";
  optAll.textContent = "全部伺服器";
  sel.appendChild(optAll);
  // 盡量保持使用者上次的選擇；切換伺服器後預設回到「目前伺服器」。
  if (historyServerFilter === "current" || historyServerFilter === "all" ||
      servers.some((s) => s.name === historyServerFilter)) {
    sel.value = historyServerFilter;
  } else {
    sel.value = "current";
    historyServerFilter = "current";
  }
  if (prev !== sel.value) refreshJobs();
}

async function refreshJobs() {
  try {
    const filter = historyFilterValue();
    const url = filter && filter !== "all"
      ? `/api/jobs?server=${encodeURIComponent(filter)}`
      : "/api/jobs";
    const r = await fetch(url);
    if (r.status === 401) { showLogin(); return; }
    jobs = await r.json();
    renderCurrentJob();
    renderHistory();
  } catch (e) {
    // 暫時連不上就等下一次刷新，不報錯打擾使用者。
  }
}

checkAuth();
