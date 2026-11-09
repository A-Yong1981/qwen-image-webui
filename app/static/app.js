"use strict";

const STATUS_TEXT = {
  queued: "排隊中",
  running: "生成中",
  completed: "已完成",
  failed: "失敗",
};

let configured = false;
let jobs = [];
let currentJobId = null;
let detailJob = null;
let authed = false;
let refreshTimer = null;

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
    $("kaggleUser").value = s.kaggle_username || "";
    $("kaggleKey").value = "";
    $("kaggleKey").placeholder = s.key_saved
      ? "已保存的金鑰（留空表示不改）"
      : "貼上 kaggle.json 裡的 key";
    $("keySavedNote").textContent = s.key_saved
      ? `金鑰已保存${s.key_hint ? "（" + s.key_hint + "）" : ""}，要換金鑰再貼新的進來。`
      : "";
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

$("saveSettingsBtn").addEventListener("click", async () => {
  const username = $("kaggleUser").value.trim();
  const key = $("kaggleKey").value.trim();
  const msg = $("settingsMsg");
  if (!username && !key) {
    msg.textContent = "沒有要改的內容，用戶名和金鑰都留空了。";
    return;
  }
  msg.textContent = "保存中……";
  const r = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, key }),
  });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (r.ok && data.ok) {
    msg.textContent = key
      ? "已保存，金鑰已保存。建議按「測試連線」確認金鑰能用。"
      : "已保存，金鑰未變。";
    $("kaggleKey").value = "";
    await loadSettings();
  } else {
    msg.textContent = "保存失敗：" + (data.error || "請稍後再試。");
  }
});

$("testBtn").addEventListener("click", async () => {
  const msg = $("settingsMsg");
  msg.textContent = "測試連線中……";
  const r = await fetch("/api/settings/test", { method: "POST" });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  msg.textContent = data.ok
    ? "連線成功，金鑰能用。"
    : "連線失敗：" + (data.error || "請檢查金鑰是否正確。");
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
      `額度剩 ${fmtSeconds(s.quota.remaining_seconds)}　` +
      `排隊 ${s.counts.queued}　進行中 ${s.counts.running}`;
    const cur = s.current_job;
    $("serverStatusCurrent").textContent = cur
      ? `目前任務：${truncateText(cur.prompt, 40)}`
      : "目前沒有進行中的任務。";
  } catch (e) {
    $("serverStatusLine").textContent = "伺服器狀態讀取失敗，請稍後再試。";
    $("serverStatusCurrent").textContent = "";
  }
}

// ---- 生圖 ----
$("generateBtn").addEventListener("click", async () => {
  const prompt = $("prompt").value.trim();
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
  msg.textContent = "提交中……";
  const r = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (r.status === 401) { showLogin(); return; }
  const data = await r.json();
  if (!r.ok) {
    msg.textContent = "提交失敗：" + (data.error || "請稍後再試。");
    return;
  }
  currentJobId = data.id;
  msg.textContent = "已排隊，一張約 4 到 5 分鐘，頁面會自動更新狀態。";
  $("prompt").value = "";
  await refreshJobs();
  await loadStatus();
});

function renderCurrentJob() {
  const box = $("currentJob");
  const job = jobs.find((j) => j.id === currentJobId) || jobs[0];
  if (!job) {
    box.classList.add("hidden");
    return;
  }
  currentJobId = job.id;
  box.classList.remove("hidden");
  $("currentStatus").textContent =
    `狀態：${STATUS_TEXT[job.status] || job.status}　建立時間：${fmtTime(job.created_at)}` +
    (job.finished_at ? `　完成時間：${fmtTime(job.finished_at)}` : "");
  $("currentPrompt").textContent = job.prompt;
  $("currentNote").textContent =
    job.status === "queued"
      ? "在排隊等前面的任務跑完，一次只跑一張。"
      : job.status === "running"
        ? "正在跑：先下載 14.23GB 模型（這步最花時間），再啟動 ComfyUI 生圖，請耐心等幾分鐘。"
        : job.status === "completed"
          ? `已完成，生圖本身用了 ${fmtSeconds(job.generation_seconds)}，` +
            `整個任務用了 ${fmtSeconds(job.gpu_seconds)}。`
          : "";
  const err = $("currentError");
  if (job.status === "failed") {
    err.textContent = "失敗原因：" + (job.error || "未知原因");
    err.classList.remove("hidden");
  } else {
    err.classList.add("hidden");
  }
  const img = $("resultImg");
  const actions = $("resultActions");
  if (job.status === "completed") {
    img.src = `/api/jobs/${job.id}/image?t=${Date.now()}`;
    img.classList.remove("hidden");
    const dl = $("downloadBtn");
    dl.href = `/api/jobs/${job.id}/image`;
    dl.setAttribute("download", `qwen-${job.id}.png`);
    actions.classList.remove("hidden");
  } else {
    img.classList.add("hidden");
    actions.classList.add("hidden");
  }
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
    const thumb =
      job.status === "completed"
        ? `<img class="thumb" src="/api/jobs/${job.id}/image" alt="縮圖" loading="lazy">`
        : '<div class="thumb none">無圖</div>';
    card.innerHTML = `
      ${thumb}
      <div class="job-info">
        <p class="prompt-label">原始提示詞</p>
        <p class="prompt">${escapeHtml(job.prompt)}</p>
        <p class="note">${STATUS_TEXT[job.status] || job.status}　${fmtTime(job.created_at)}</p>
        ${job.status === "failed" && job.error
          ? `<p class="error">失敗原因：${escapeHtml(job.error)}</p>` : ""}
      </div>`;
    card.addEventListener("click", () => showDetail(job));
    if (job.status === "completed" || job.status === "failed") {
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
    if (detailJob && detailJob.id === job.id) {
      $("historyDetail").classList.add("hidden");
      detailJob = null;
    }
    if (currentJobId === job.id) currentJobId = null;
    msg.textContent = "已刪除。";
    await refreshJobs();
  } catch (e) {
    msg.textContent = "刪除失敗，請稍後再試。";
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
    $("historyDetail").classList.add("hidden");
    detailJob = null;
    msg.textContent = `已清空 ${data.deleted || 0} 條記錄。`;
    await refreshJobs();
  } catch (e) {
    msg.textContent = "清空失敗，請稍後再試。";
  }
});

$("detailDeleteBtn").addEventListener("click", () => {
  if (detailJob) deleteJob(detailJob);
});

function showDetail(job) {
  detailJob = job;
  $("historyDetail").classList.remove("hidden");
  $("detailPrompt").textContent = job.prompt;
  $("copyMsg").textContent = "";
  $("detailMeta").textContent =
    `${STATUS_TEXT[job.status] || job.status}　建立：${fmtTime(job.created_at)}` +
    `　完成：${fmtTime(job.finished_at)}　生圖用時：${fmtSeconds(job.generation_seconds)}` +
    `　任務總用時：${fmtSeconds(job.gpu_seconds)}`;
  const img = $("detailImg");
  const dl = $("detailDownload");
  const err = $("detailError");
  $("detailDeleteBtn").style.display =
    (job.status === "completed" || job.status === "failed") ? "" : "none";
  if (job.status === "completed") {
    img.src = `/api/jobs/${job.id}/image?t=${Date.now()}`;
    img.style.display = "";
    dl.href = `/api/jobs/${job.id}/image`;
    dl.setAttribute("download", `qwen-${job.id}.png`);
    dl.style.display = "";
    err.classList.add("hidden");
  } else {
    img.style.display = "none";
    dl.style.display = "none";
    err.textContent = job.status === "failed"
      ? "失敗原因：" + (job.error || "未知原因")
      : "這張圖還沒有產生。";
    err.classList.remove("hidden");
  }
  $("historyDetail").scrollIntoView({ behavior: "smooth" });
}

$("detailClose").addEventListener("click", () => {
  $("historyDetail").classList.add("hidden");
  detailJob = null;
});

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
  if (!detailJob) return;
  const ok = await copyText(detailJob.prompt);
  $("copyMsg").textContent = ok ? "已複製提示詞。" : "複製失敗，請手動選取文字複製。";
});

$("reusePromptBtn").addEventListener("click", () => {
  if (!detailJob) return;
  $("prompt").value = detailJob.prompt;
  $("historyDetail").classList.add("hidden");
  showTab("generate");
  $("prompt").focus();
  $("prompt").scrollIntoView({ behavior: "smooth", block: "center" });
});

// ---- 刷新 ----
async function refreshJobs() {
  try {
    const r = await fetch("/api/jobs");
    if (r.status === 401) { showLogin(); return; }
    jobs = await r.json();
    renderCurrentJob();
    renderHistory();
  } catch (e) {
    // 暫時連不上就等下一次刷新，不報錯打擾使用者。
  }
}

checkAuth();
