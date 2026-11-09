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
let previewJob = null;
let displayedJob = null;
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
  single: "只選 1 張圖（jpg、png、webp，每張不超過 10MB）。這張圖決定出圖的底子，會按你選的尺寸先縮好。",
  multi: "最多選 4 張圖（jpg、png、webp，每張不超過 10MB）。第一張圖決定出圖的底子，後面的圖提供內容（比如衣服、姿勢、背景）。",
};
const MODE_LABEL = { t2i: "純文字生圖", single: "單圖參考", multi: "多圖參考" };
let genMode = "t2i";
let refFiles = [];

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
  if (opts.mode && opts.mode !== "t2i" &&
      opts.ref_strength !== undefined && opts.ref_strength !== null) {
    parts.push(`參考強度 ${opts.ref_strength}`);
  }
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
  $("refStrengthRow").classList.toggle("hidden", !isRef);
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
    ? "生圖要求（寫清楚要怎麼改參考圖）"
    : "生圖要求（中文就可以，直接寫你想要的畫面）";
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

$("refStrength").addEventListener("input", () => {
  $("refStrengthVal").textContent = Number($("refStrength").value).toFixed(2);
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
    ref_strength: $("refStrength").value,
  };
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
    form.append("ref_strength", options.ref_strength);
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
    (job.finished_at ? `　完成時間：${fmtTime(job.finished_at)}` : "");
  $("currentPrompt").textContent = job.prompt;
  $("currentOptions").textContent = fmtOptionsFull(job.options);
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
  // 預覽區的刪除按鈕只對已完成的任務顯示。
  $("detailDeleteBtn").style.display = job.status === "completed" ? "" : "none";
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
        ${job.options ? `<p class="note">${escapeHtml(fmtOptions(job.options))}</p>` : ""}
        <p class="note">${STATUS_TEXT[job.status] || job.status}　${fmtTime(job.created_at)}</p>
        ${job.status === "failed" && job.error
          ? `<p class="error">失敗原因：${escapeHtml(job.error)}</p>` : ""}
      </div>`;
    card.addEventListener("click", () => showPreview(job));
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
  if (opts.ref_strength !== undefined && opts.ref_strength !== null) {
    $("refStrength").value = opts.ref_strength;
    $("refStrengthVal").textContent = Number(opts.ref_strength).toFixed(2);
  }
  if (opts.mode && opts.mode !== "t2i") {
    $("generateMsg").textContent = "已帶回上次的設定，參考圖請重新上傳。";
  }
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
