#!/usr/bin/env python3
"""Qwen 生圖工作台後端：FastAPI + SQLite + 後台執行緒調 Kaggle API。

只依賴 fastapi / uvicorn，其餘全是標準庫（sqlite3、urllib、json、threading）。
流程（已在 Kaggle T4 x2 上跑通過，v4 COMPLETE）：
  前端提交提示詞 -> 後端把提示詞注入核心腳本模板 -> SaveKernel 提交新版本
  -> 每 60 秒查狀態 -> COMPLETE 後下載 PNG + test_summary.json -> 任務完成。
金鑰只存在 /data/settings.json（權限 600）或環境變數，絕不寫進日誌、
絕不回傳給前端。401 一律只回「金鑰無效或過期，請在設定頁重新填寫」。
"""
import base64
import json
import os
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_URL = "https://www.kaggle.com/api/v1"
KERNEL_OWNER = "xiongyong5207"
KERNEL_SLUG = "qwen-image-21-gguf-t4-test"
WEEKLY_QUOTA_SECONDS = 108000
POLL_INTERVAL_SECONDS = 60
MAX_WAIT_SECONDS = 2 * 3600

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jobs.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_PATH = Path(__file__).resolve().parent / "kernel_template.py"

KEY_ERROR_TEXT = "金鑰無效或過期，請在設定頁重新填寫"
NO_KEY_TEXT = "尚未填寫 Kaggle 金鑰，請先去設定頁填寫"


class KaggleAuthError(RuntimeError):
    """Kaggle 回了 401：金鑰不對或過期。"""


class KaggleError(RuntimeError):
    """Kaggle 請求失敗（不含任何憑證內容）。"""


# ---------------------------------------------------------------- 憑證
def load_settings():
    """先讀環境變數，再讓設定檔覆蓋（設定頁是主要填寫入口）。"""
    settings = {"username": "", "key": ""}
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        settings = {"username": os.environ["KAGGLE_USERNAME"],
                    "key": os.environ["KAGGLE_KEY"]}
    try:
        if SETTINGS_PATH.exists():
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("username") and saved.get("key"):
                settings = {"username": saved["username"], "key": saved["key"]}
    except Exception:
        pass
    return settings


def save_settings(username, key):
    """寫 /data/settings.json，權限 600。值絕不寫進日誌。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(SETTINGS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(
            {"username": username, "key": key}, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- Kaggle HTTP
def _auth_header(settings):
    raw = f"{settings['username']}:{settings['key']}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _open(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw.strip():
                return {}
            return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise KaggleAuthError("unauthorized")
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        raise KaggleError(f"Kaggle 回 HTTP {e.code}：{body}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KaggleError(f"連不上 Kaggle：{e}")


def kaggle_post(settings, path, body, timeout=60):
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": _auth_header(settings)})
    return _open(req, timeout)


def kaggle_get(settings, path, timeout=60):
    req = urllib.request.Request(
        BASE_URL + path,
        headers={"Accept": "application/json",
                 "Authorization": _auth_header(settings)})
    return _open(req, timeout)


def download_bytes(url, timeout=600):
    """下載 Kaggle 輸出檔。輸出連結是帶簽名的直連位址，不用帶認證。"""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise KaggleError(f"下載輸出檔失敗，HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KaggleError(f"下載輸出檔失敗：{e}")


def download_zip(settings, timeout=600):
    """備用路徑：整包下載輸出（zip）。"""
    req = urllib.request.Request(
        f"{BASE_URL}/kernels/output/{KERNEL_OWNER}/{KERNEL_SLUG}",
        headers={"Authorization": _auth_header(settings)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise KaggleAuthError("unauthorized")
        raise KaggleError(f"下載輸出包失敗，HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KaggleError(f"下載輸出包失敗：{e}")


# ---------------------------------------------------------------- 資料庫
DB_LOCK = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        conn = _conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            gpu_seconds REAL,
            generation_seconds REAL,
            error TEXT,
            kernel_version INTEGER)""")
        conn.commit()
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_to_job(row):
    return {"id": row["id"], "prompt": row["prompt"], "status": row["status"],
            "created_at": row["created_at"], "finished_at": row["finished_at"],
            "gpu_seconds": row["gpu_seconds"],
            "generation_seconds": row["generation_seconds"],
            "error": row["error"], "kernel_version": row["kernel_version"]}


def create_job(prompt):
    job_id = uuid.uuid4().hex[:12]
    with DB_LOCK:
        conn = _conn()
        conn.execute("INSERT INTO jobs (id, prompt, status, created_at) "
                     "VALUES (?, ?, 'queued', ?)", (job_id, prompt, _now()))
        conn.commit()
        conn.close()
    return job_id


def get_job(job_id):
    with DB_LOCK:
        conn = _conn()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
    return _row_to_job(row) if row else None


def list_jobs():
    with DB_LOCK:
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        conn.close()
    return [_row_to_job(r) for r in rows]


def next_queued_job():
    with DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' "
            "ORDER BY created_at ASC LIMIT 1").fetchone()
        conn.close()
    return _row_to_job(row) if row else None


def update_job(job_id, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    with DB_LOCK:
        conn = _conn()
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                     (*fields.values(), job_id))
        conn.commit()
        conn.close()


def fail_job(job_id, error):
    update_job(job_id, status="failed", finished_at=_now(), error=error[:500])


def complete_job(job_id, generation_seconds, gpu_seconds):
    update_job(job_id, status="completed", finished_at=_now(),
               generation_seconds=generation_seconds, gpu_seconds=gpu_seconds)


# ---------------------------------------------------------------- 核心腳本
def build_kernel_script(prompt):
    """把使用者提示詞注入模板。json.dumps 轉義，絕不用字串拼接。"""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count("__USER_PROMPT_JSON__") != 1:
        raise RuntimeError("核心腳本模板佔位符異常")
    return template.replace("__USER_PROMPT_JSON__",
                            json.dumps(prompt, ensure_ascii=True))


def extract_error_log(output):
    """從 ListKernelSessionOutput 的 log 取最後 500 字（只含 Kaggle 日誌）。"""
    log = output.get("log") if isinstance(output, dict) else None
    text = ""
    if isinstance(log, str):
        try:
            entries = json.loads(log)
            if isinstance(entries, list):
                text = "".join(str(e.get("data", "")) for e in entries
                               if isinstance(e, dict))
            else:
                text = log
        except Exception:
            text = log
    elif isinstance(log, list):
        text = "".join(str(e.get("data", "")) for e in log
                       if isinstance(e, dict))
    return text[-500:] if text else ""


def download_outputs(job_id, settings, output):
    """下載 PNG + test_summary.json + hardware.json。先走檔案連結，失敗再走整包 zip。"""
    files = output.get("files") or []
    by_name = {}
    for f in files:
        if not isinstance(f, dict):
            continue
        name = f.get("fileName") or f.get("file_name") or ""
        url = f.get("url") or f.get("downloadUrl") or f.get("fileUrl") or ""
        if name:
            by_name[name] = url
    png_bytes = None
    summary = {}
    if by_name.get("qwen_test_chufeng.png"):
        png_bytes = download_bytes(by_name["qwen_test_chufeng.png"])
    if by_name.get("test_summary.json"):
        summary = json.loads(
            download_bytes(by_name["test_summary.json"]).decode("utf-8"))
    if by_name.get("hardware.json"):
        (JOBS_DIR / f"{job_id}.hardware.json").write_bytes(
            download_bytes(by_name["hardware.json"]))
    if png_bytes is None:
        zip_bytes = download_zip(settings)
        with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
            names = z.namelist()
            png_name = next((n for n in names
                            if n.endswith("qwen_test_chufeng.png")), None)
            if png_name is None:
                raise KaggleError("Kaggle 輸出裡沒有找到圖片")
            png_bytes = z.read(png_name)
            s_name = next((n for n in names
                           if n.endswith("test_summary.json")), None)
            if s_name:
                summary = json.loads(z.read(s_name).decode("utf-8"))
            h_name = next((n for n in names
                           if n.endswith("hardware.json")), None)
            if h_name:
                (JOBS_DIR / f"{job_id}.hardware.json").write_bytes(z.read(h_name))
    if not png_bytes or not png_bytes.startswith(b"\x89PNG"):
        raise KaggleError("下載到的圖片不是有效的 PNG")
    (JOBS_DIR / f"{job_id}.png").write_bytes(png_bytes)
    if summary:
        (JOBS_DIR / f"{job_id}.summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_job(job):
    """跑一個任務：提交 -> 輪詢 -> 下載。任何 401 都只記金鑰無效。"""
    job_id = job["id"]
    settings = load_settings()
    if not settings["username"] or not settings["key"]:
        fail_job(job_id, NO_KEY_TEXT)
        return
    try:
        script = build_kernel_script(job["prompt"])
        body = {
            "slug": KERNEL_REF,
            "newTitle": KERNEL_SLUG,
            "text": script,
            "language": "python",
            "kernelType": "script",
            "isPrivate": True,
            "enableGpu": True,
            "enableTpu": False,
            "enableInternet": True,
            "datasetDataSources": [],
            "kernelDataSources": [],
            "competitionDataSources": [],
            "modelDataSources": [],
            "categoryIds": [],
            "sessionTimeoutSeconds": 7200,
            "machineShape": "NvidiaTeslaT4",
        }
        resp = kaggle_post(settings, "/kernels.KernelsApiService/SaveKernel",
                           body, timeout=120)
        version = resp.get("versionNumber") if isinstance(resp, dict) else None
        update_job(job_id, status="running", kernel_version=version)
        status = None
        deadline = time.time() + MAX_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            st = kaggle_post(
                settings, "/kernels.KernelsApiService/GetKernelSessionStatus",
                {"userName": KERNEL_OWNER, "kernelSlug": KERNEL_SLUG}, timeout=60)
            status = (st or {}).get("status") if isinstance(st, dict) else None
            if status in ("COMPLETE", "ERROR", "FAILED", "CANCELLED"):
                break
            # 狀態為空（剛提交還沒分到機器，v4 時空了約 17 分鐘）就繼續等。
        if status != "COMPLETE":
            if status in ("ERROR", "FAILED", "CANCELLED"):
                try:
                    out = kaggle_post(
                        settings,
                        "/kernels.KernelsApiService/ListKernelSessionOutput",
                        {"userName": KERNEL_OWNER, "kernelSlug": KERNEL_SLUG,
                         "pageSize": 20}, timeout=60)
                    err = extract_error_log(out)
                except Exception:
                    err = ""
                fail_job(job_id, err or "Kaggle 執行失敗，請看設定頁的金鑰是否正確")
            else:
                fail_job(job_id, "等待超時（2 小時），任務未完成")
            return
        out = kaggle_post(
            settings, "/kernels.KernelsApiService/ListKernelSessionOutput",
            {"userName": KERNEL_OWNER, "kernelSlug": KERNEL_SLUG,
             "pageSize": 20}, timeout=60)
        summary = download_outputs(job_id, settings, out)
        gen = summary.get("generation_seconds") if isinstance(summary, dict) else None
        total = summary.get("total_seconds") if isinstance(summary, dict) else None
        complete_job(job_id, generation_seconds=gen, gpu_seconds=total)
    except KaggleAuthError:
        fail_job(job_id, KEY_ERROR_TEXT)
    except KaggleError as e:
        fail_job(job_id, str(e)[:500])
    except Exception:
        traceback.print_exc()
        fail_job(job_id, "後端內部錯誤，請稍後再試一次")


def worker_loop():
    """後台執行緒：一次只跑一個任務，後面的排隊等。"""
    while True:
        try:
            job = next_queued_job()
            if job:
                run_job(job)
            else:
                time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


# ---------------------------------------------------------------- API
@asynccontextmanager
async def lifespan(app):
    init_db()
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    yield


app = FastAPI(title="Qwen 生圖工作台", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/jobs")
async def create_job_endpoint(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    prompt = (data.get("prompt") or "").strip() if isinstance(data, dict) else ""
    if not prompt:
        return JSONResponse({"error": "提示詞不能為空"}, status_code=400)
    if len(prompt) > 2000:
        return JSONResponse({"error": "提示詞太長，請控制在 2000 字以內"},
                            status_code=400)
    job_id = create_job(prompt)
    return {"id": job_id, "status": "queued"}


@app.get("/api/jobs")
def list_jobs_endpoint():
    return list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job_endpoint(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse({"error": "找不到這個任務"}, status_code=404)
    return job


@app.get("/api/jobs/{job_id}/image")
def get_image(job_id: str):
    path = JOBS_DIR / f"{job_id}.png"
    if not path.exists():
        return JSONResponse({"error": "圖片還沒有產生"}, status_code=404)
    return FileResponse(path, media_type="image/png",
                        filename=f"qwen-{job_id}.png")


@app.get("/api/quota")
def quota():
    with DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(gpu_seconds), 0) FROM jobs "
            "WHERE status = 'completed'").fetchone()
        conn.close()
    used = float(row[0] or 0)
    return {"used_seconds": used, "total_seconds": WEEKLY_QUOTA_SECONDS,
            "remaining_seconds": max(0, WEEKLY_QUOTA_SECONDS - used)}


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    return {"kaggle_configured": bool(s["username"] and s["key"]),
            "kaggle_username": s["username"]}


@app.post("/api/settings")
async def save_settings_endpoint(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    username = (data.get("username") or "").strip() if isinstance(data, dict) else ""
    key = (data.get("key") or "").strip() if isinstance(data, dict) else ""
    if not username or not key:
        return JSONResponse({"error": "用戶名和金鑰都要填"}, status_code=400)
    save_settings(username, key)
    return {"ok": True}


@app.post("/api/settings/test")
def test_settings():
    s = load_settings()
    if not s["username"] or not s["key"]:
        return {"ok": False, "error": NO_KEY_TEXT}
    try:
        kaggle_get(s, "/kernels/list?user=" +
                   urllib.parse.quote(s["username"], safe=""), timeout=30)
        return {"ok": True}
    except KaggleAuthError:
        return {"ok": False, "error": KEY_ERROR_TEXT}
    except KaggleError as e:
        return {"ok": False, "error": str(e)[:200]}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
