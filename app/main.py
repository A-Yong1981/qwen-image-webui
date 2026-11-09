#!/usr/bin/env python3
"""Qwen 生圖工作台後端：FastAPI + SQLite + 後台執行緒調 Kaggle API。

只依賴 fastapi / uvicorn / python-multipart / Pillow，其餘全是標準庫
（sqlite3、urllib、json、threading、hmac、hashlib）。
流程（已在 Kaggle T4 x2 上跑通過，v4 COMPLETE）：
  前端提交提示詞 -> 後端把提示詞注入核心腳本模板 -> SaveKernel 提交新版本
  -> 每 60 秒查狀態 -> COMPLETE 後下載 PNG + test_summary.json -> 任務完成。
金鑰只存在 /data/settings.json（權限 600）或環境變數，絕不寫進日誌、
絕不回傳給前端。401 一律只回「金鑰無效或過期，請在設定頁重新填寫」。
登入：帳號密碼。帳號密碼的雜湊存在 /data/auth.json（權限 600），密碼
本身絕不存明文、絕不寫進日誌。登入後發 24 小時 HMAC 簽名的登入憑證
（cookie）；改密碼會換簽名密鑰，舊憑證立刻失效，要重新登入。
預設帳號 anyong、預設密碼 admin，登入後請到設定頁改掉。
v2.5：多伺服器——一個伺服器就是一個 Kaggle 帳號（名字、用戶名、
金鑰、可選的數據集），存在 /data/settings.json，右上角切換；
有配數據集的伺服器，內核提交時掛上數據集就不用再下載 14.23GB
模型（見 kernel_template.py 第 3 步），沒配數據集的伺服器走
下載老路。任務建立時記下是哪個伺服器跑的，換伺服器不影響
已經排隊的任務。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
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

import ipaddress
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

BASE_URL = "https://www.kaggle.com/api/v1"
KERNEL_OWNER = "xiongyong5207"
KERNEL_SLUG = "qwen-image-21-gguf-t4-test"
# 內核的完整引用是 {伺服器用戶名}/{KERNEL_SLUG}，每個伺服器
# 各有自己的內核（帳號不同，內核就不同）；不要再寫死單一引用。
WEEKLY_QUOTA_SECONDS = 108000
POLL_INTERVAL_SECONDS = 60
MAX_WAIT_SECONDS = 2 * 3600
START_TIME = time.time()

# 登入憑證 24 小時。
SESSION_TTL_SECONDS = 24 * 3600
SESSION_COOKIE = "qwen_session"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jobs.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
AUTH_PATH = DATA_DIR / "auth.json"
JOBS_DIR = DATA_DIR / "jobs"
REFS_DIR = DATA_DIR / "refs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_PATH = Path(__file__).resolve().parent / "kernel_template.py"

# 參考圖：官方範本最多 10 張，本工作台先做到最多 4 張。
MAX_REFS = 4
MAX_REF_SIZE_MB = 10
MAX_REF_SIZE_BYTES = MAX_REF_SIZE_MB * 1024 * 1024
MAX_REF_EDGE = 1024
REF_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
REF_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
REF_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

# 比例 -> 寬 x 高（全部是 32 的倍數，出圖尺寸用這個填）。
ASPECT_SIZES = {
    "1:1": (768, 768),
    "16:9": (1024, 576),
    "9:16": (576, 1024),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
    "3:2": (1024, 672),
    "2:3": (672, 1024),
}

KEY_ERROR_TEXT = "金鑰無效或過期，請在設定頁重新填寫"
NO_KEY_TEXT = "尚未填寫 Kaggle 金鑰，請先去設定頁填寫"
NOT_LOGGED_IN_TEXT = "請先登入"


class KaggleAuthError(RuntimeError):
    """Kaggle 回了 401：金鑰不對或過期。"""


class KaggleError(RuntimeError):
    """Kaggle 請求失敗（不含任何憑證內容）。"""


# ---------------------------------------------------------------- 伺服器（多 Kaggle 帳號）
# settings.json v2.5 格式：
#   {"version": 3, "current_server_id": "<id>", "servers": [
#     {"id": "<id>", "name": "主號", "username": "xiongyong5207",
#      "key": "...", "dataset": "xiongyong5207/qwen-image-21-models"}]}
# 一個伺服器就是一個 Kaggle 帳號，各有自己的每週 GPU 額度。
# dataset 是這個帳號自己的私有數據集（模型緩存），可留空；
# 私有數據集只對自己的帳號可見，別的帳號填了也掛不上。
# 舊格式 {"username", "key"} 第一次讀到時自動遷移成一個「主號」
# 伺服器，金鑰一個字都不會丟。
DEFAULT_DATASET_REF = "xiongyong5207/qwen-image-21-models"


def _new_server(name, username, key, dataset=""):
    return {"id": "srv-" + uuid.uuid4().hex[:8], "name": name,
            "username": (username or "").strip(),
            "key": (key or "").strip(), "dataset": (dataset or "").strip()}


def _default_dataset_for(username):
    """預設數據集：只有 xiongyong5207 這個帳號才預填，
    私有數據集只對自己的帳號可見，別的帳號預填了也掛不上。"""
    return DEFAULT_DATASET_REF if username == KERNEL_OWNER else ""


def _clean_server(raw):
    """把一個伺服器記錄整理成合法形狀；缺 id 就補一個。"""
    if not isinstance(raw, dict):
        return None
    srv = _new_server(raw.get("name") or "未命名",
                      raw.get("username"), raw.get("key"),
                      raw.get("dataset"))
    if raw.get("id"):
        srv["id"] = str(raw["id"])
    return srv


def _settings_from_env():
    """環境變數 KAGGLE_USERNAME / KAGGLE_KEY：只有在設定檔完全
    不存在時才用（第一次啟動、還沒在設定頁填過的情況）。"""
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        srv = _new_server("主號", os.environ["KAGGLE_USERNAME"],
                          os.environ["KAGGLE_KEY"],
                          _default_dataset_for(os.environ["KAGGLE_USERNAME"]))
        return {"version": 3, "current_server_id": srv["id"],
                "servers": [srv]}
    return {"version": 3, "current_server_id": None, "servers": []}


def load_settings():
    """讀 /data/settings.json，回傳 v2.5 格式的完整設定
    （含金鑰，只給後端內部用；回前端的接口另外過濾）。

    舊格式 {"username", "key"}：自動遷移成一個「主號」伺服器，
    數據集預填（見 _default_dataset_for），並把遷移後的檔案
    寫回去；舊金鑰原封不動。"""
    try:
        if SETTINGS_PATH.exists():
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict) and isinstance(saved.get("servers"), list):
                servers = [s for s in (_clean_server(r)
                                       for r in saved["servers"]) if s]
                current = saved.get("current_server_id")
                if current not in [s["id"] for s in servers]:
                    current = servers[0]["id"] if servers else None
                return {"version": 3, "current_server_id": current,
                        "servers": servers}
            if isinstance(saved, dict) and saved.get("username") \
                    and saved.get("key"):
                srv = _new_server("主號", saved["username"], saved["key"],
                                  _default_dataset_for(saved["username"]))
                migrated = {"version": 3, "current_server_id": srv["id"],
                            "servers": [srv]}
                save_settings_full(migrated)
                return migrated
    except Exception:
        pass
    return _settings_from_env()


def save_settings_full(settings):
    """寫整個 /data/settings.json，權限 600。值絕不寫進日誌。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(SETTINGS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(settings, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


def current_server(settings=None):
    """目前伺服器（右上角切換器選中的那個）；沒有就回第一個，
    一個都沒有就回 None。"""
    settings = settings or load_settings()
    servers = settings.get("servers") or []
    if not servers:
        return None
    for srv in servers:
        if srv["id"] == settings.get("current_server_id"):
            return srv
    return servers[0]


def find_server(settings, server_id):
    for srv in settings.get("servers") or []:
        if srv["id"] == server_id:
            return srv
    return None


def find_server_by_name(settings, name):
    for srv in settings.get("servers") or []:
        if srv["name"] == name:
            return srv
    return None


def _key_hint(key):
    key = key or ""
    if len(key) >= 8:
        return "••••" + key[-4:]
    if key:
        return "••••"
    return ""


def public_server(srv):
    """回給前端的伺服器資料：絕不含完整金鑰，只回 key_saved 與後 4 位。"""
    return {"id": srv["id"], "name": srv["name"],
            "username": srv["username"], "dataset": srv.get("dataset") or "",
            "key_saved": bool(srv.get("key")),
            "key_hint": _key_hint(srv.get("key"))}


# ---------------------------------------------------------------- 帳號密碼登入
# 帳號密碼存在 /data/auth.json（權限 600），格式：
#   {"version": 2, "username": "anyong", "password_hash": "...",
#    "salt": "...", "session_secret": "..."}
# 密碼絕不存明文：PBKDF2-HMAC-SHA256，200000 次，鹽 16 位元組。
# 密碼值絕不寫進日誌。改密碼時換 session_secret，舊登入憑證立刻失效。
DEFAULT_USERNAME = "anyong"
DEFAULT_PASSWORD = "admin"
PBKDF2_ITERATIONS = 200000
MIN_PASSWORD_LENGTH = 4


def _write_json_600(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hash_password(password, salt_hex):
    """PBKDF2-HMAC-SHA256，200000 次，回傳雜湊的 hex。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        bytes.fromhex(salt_hex), PBKDF2_ITERATIONS).hex()


def load_auth():
    """讀 /data/auth.json；沒有、損壞、或是舊版 OTP 格式（有 totp_secret），
    就初始化成預設帳號 anyong、密碼 admin。"""
    try:
        if AUTH_PATH.exists():
            with open(AUTH_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            if (saved.get("version") == 2 and saved.get("username")
                    and saved.get("password_hash") and saved.get("salt")
                    and saved.get("session_secret")):
                return {"version": 2, "username": saved["username"],
                        "password_hash": saved["password_hash"],
                        "salt": saved["salt"],
                        "session_secret": saved["session_secret"]}
    except Exception:
        pass
    salt = secrets.token_hex(16)
    auth = {"version": 2, "username": DEFAULT_USERNAME,
            "password_hash": _hash_password(DEFAULT_PASSWORD, salt),
            "salt": salt, "session_secret": secrets.token_hex(32)}
    _write_json_600(AUTH_PATH, auth)
    return auth


def save_auth(auth):
    _write_json_600(AUTH_PATH, auth)


def _verify_password(auth, password):
    """驗密碼：雜湊比對，時間固定，比對失敗不透露是哪一項錯。"""
    if not isinstance(password, str) or not password:
        return False
    try:
        expect = _hash_password(password, auth["salt"])
    except Exception:
        return False
    return hmac.compare_digest(expect, auth["password_hash"])


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_token(auth, exp):
    """發一個 HMAC 簽名的登入憑證：payload.簽名。只簽 session 一種。"""
    payload = json.dumps(
        {"t": "session", "exp": int(exp)},
        separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url(payload)
    sig = hmac.new(auth["session_secret"].encode("ascii"),
                   payload_b64.encode("ascii"),
                   hashlib.sha256).digest()
    return payload_b64 + "." + _b64url(sig)


def _verify_token(auth, token):
    """驗登入憑證：簽名對得上、種類對、沒過期，才回 payload，否則回 None。"""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expect = hmac.new(auth["session_secret"].encode("ascii"),
                          payload_b64.encode("ascii"),
                          hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expect), sig_b64):
            return None
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8"))
        if payload.get("t") != "session":
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _set_session_cookie(resp, auth):
    """發登入憑證（24 小時）。"""
    session_exp = int(time.time()) + SESSION_TTL_SECONDS
    resp.set_cookie(SESSION_COOKIE,
                    _sign_token(auth, session_exp),
                    max_age=SESSION_TTL_SECONDS, httponly=True,
                    samesite="lax", path="/")
    return resp


def _clear_auth_cookies(resp):
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _auth_state(request):
    """回 (authenticated, username)。只認 24 小時登入憑證。"""
    auth = load_auth()
    if _verify_token(auth, request.cookies.get(SESSION_COOKIE, "")):
        return True, auth["username"]
    return False, ""


def _require_auth(request):
    authenticated, _ = _auth_state(request)
    if not authenticated:
        return JSONResponse({"error": NOT_LOGGED_IN_TEXT}, status_code=401)
    return None


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


def download_zip(settings, kernel_owner, timeout=600):
    """備用路徑：整包下載輸出（zip）。內核屬於哪個帳號就從哪個帳號下載。"""
    req = urllib.request.Request(
        f"{BASE_URL}/kernels/output/{kernel_owner}/{KERNEL_SLUG}",
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
    REFS_DIR.mkdir(parents=True, exist_ok=True)
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
            kernel_version INTEGER,
            options TEXT,
            server TEXT)""")
        # v2.4：舊庫沒有 options 欄位，第一次啟動時補上。
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(jobs)").fetchall()]
        if "options" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN options TEXT")
        # v2.5：舊庫沒有 server 欄位，第一次啟動時補上。
        if "server" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN server TEXT")
        # 舊任務（或沒記下伺服器的任務）都是主號跑的，補上名字，
        # 額度統計才不會漏；每次啟動都補，漏網的也補得到。
        conn.execute(
            "UPDATE jobs SET server = '主號' "
            "WHERE server IS NULL OR server = ''")
        conn.commit()
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_to_job(row):
    try:
        raw_options = row["options"]
    except (IndexError, KeyError):
        raw_options = None
    try:
        options = json.loads(raw_options) if raw_options else None
    except Exception:
        options = None
    try:
        server = row["server"]
    except (IndexError, KeyError):
        server = None
    return {"id": row["id"], "prompt": row["prompt"], "status": row["status"],
            "created_at": row["created_at"], "finished_at": row["finished_at"],
            "gpu_seconds": row["gpu_seconds"],
            "generation_seconds": row["generation_seconds"],
            "error": row["error"], "kernel_version": row["kernel_version"],
            "options": options, "server": server}


def create_job(prompt, options=None, job_id=None, server=None):
    """建一個任務。server 是跑這個任務的伺服器名字，
    建立時就記下來，之後換伺服器也不影響它。"""
    job_id = job_id or uuid.uuid4().hex[:12]
    opts_json = (json.dumps(options, ensure_ascii=False)
                 if options else None)
    with DB_LOCK:
        conn = _conn()
        conn.execute("INSERT INTO jobs (id, prompt, status, created_at, options, server) "
                     "VALUES (?, ?, 'queued', ?, ?, ?)",
                     (job_id, prompt, _now(), opts_json, server or ""))
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


def get_job_status(job_id):
    """只讀一個任務的狀態（取消檢查用）。"""
    with DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
    return row["status"] if row else None


def try_start_job(job_id, version):
    """把任務從 queued 改成 running，只在它還是 queued 時才改。
    回傳 True 表示改成功（可以繼續跑）；False 表示它剛被取消了。"""
    with DB_LOCK:
        conn = _conn()
        cur = conn.execute(
            "UPDATE jobs SET status = 'running', kernel_version = ? "
            "WHERE id = ? AND status = 'queued'",
            (version, job_id))
        conn.commit()
        changed = cur.rowcount
        conn.close()
    return changed > 0


def fail_job(job_id, error):
    update_job(job_id, status="failed", finished_at=_now(), error=error[:500])


def complete_job(job_id, generation_seconds, gpu_seconds):
    update_job(job_id, status="completed", finished_at=_now(),
               generation_seconds=generation_seconds, gpu_seconds=gpu_seconds)


# ---------------------------------------------------------------- 生圖選項
def _clamp_int(value, lo, hi, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_float(value, lo, hi, default):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_size(value):
    """出圖寬高：夾到 256-1280，再往下取整到 32 的倍數
    （ComfyUI 的 latent 以 32 為單位，編輯模式畫布也吃 32 的倍數）。"""
    n = _clamp_int(value, 256, 1280, 768)
    return max(256, min(1280, (n // 32) * 32))


def _aspect_label(width, height):
    """從寬高反推比例名稱，七種比例都對不上就叫「自定」。"""
    if not height:
        return "自定"
    ratio = width / height
    for name, (aw, ah) in ASPECT_SIZES.items():
        if abs(ratio - aw / ah) < 0.01:
            return name
    return "自定"


def _normalize_options(raw, ref_count=None, ref_urls=None):
    """把前端傳來的選項整理成一份完整的記錄。

    全部欄位都有預設值（沿用已驗證的 v4 配方），缺了、填錯了、
    超出範圍了都自動回到合法值，不會因為一個欄位填錯就整單失敗。
    種子留空表示隨機，後端隨機一個並存進記錄，歷史裡看得到，
    同一句話加同一個種子就能複現同一張圖。"""
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "t2i").strip()
    if mode not in ("t2i", "single", "multi"):
        mode = "t2i"
    aspect = str(raw.get("aspect") or "1:1").strip()
    default_w, default_h = ASPECT_SIZES.get(aspect, (768, 768))
    width = _clamp_size(raw.get("width") if raw.get("width") not in (None, "")
                        else default_w)
    height = _clamp_size(raw.get("height") if raw.get("height") not in (None, "")
                         else default_h)
    seed_raw = raw.get("seed")
    if seed_raw in (None, ""):
        seed = secrets.randbelow(10 ** 15)
    else:
        seed = _clamp_int(seed_raw, 0, 10 ** 15 - 1,
                          secrets.randbelow(10 ** 15))
    steps = _clamp_int(raw.get("steps"), 10, 50, 20)
    cfg = _clamp_float(raw.get("cfg"), 1.0, 4.0, 1.0)
    ref_strength = _clamp_float(raw.get("ref_strength"), 0.0, 1.0, 0.7)
    return {
        "mode": mode,
        "ref_count": int(ref_count if ref_count is not None
                         else raw.get("ref_count") or 0),
        "ref_urls": list(ref_urls if ref_urls is not None
                         else raw.get("ref_urls") or []),
        "aspect": _aspect_label(width, height),
        "width": width,
        "height": height,
        "seed": seed,
        "steps": steps,
        "cfg": cfg,
        "ref_strength": ref_strength,
        # 這個任務要不要掛數據集（由建立時選中的伺服器決定，
        # 有配數據集才 True）；內核腳本第 3 步看這個值分流。
        "use_dataset": bool(raw.get("use_dataset")),
    }


def _is_private_host(host):
    """本機或內網位址：參考圖網址不能用這種位址，Kaggle 那邊打不開。"""
    host = (host or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False  # 是域名不是 IP，當作公網處理。


def _public_base_url(request):
    """參考圖要給 Kaggle 內核下載，必須是公網能打開的完整網址。

    優先用使用者現在打開網站的網址（走隧道進來的就是公網域名）；
    如果是本機或內網位址，就用環境變數 REF_BASE_URL，
    沒設就預設用隧道的域名。"""
    host = request.url.hostname or ""
    if host and not _is_private_host(host):
        return str(request.base_url).rstrip("/")
    return os.environ.get("REF_BASE_URL",
                          "https://qwen.aynas.de5.net").rstrip("/")


# ---------------------------------------------------------------- 核心腳本
def build_kernel_script(prompt, options=None):
    """把使用者提示詞與生圖選項注入模板。json.dumps 轉義，絕不用字串拼接。"""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count("__USER_PROMPT_JSON__") != 1:
        raise RuntimeError("核心腳本模板佔位符異常")
    if template.count("__JOB_OPTIONS_JSON__") != 1:
        raise RuntimeError("核心腳本模板佔位符異常")
    opts = _normalize_options(options or {})
    # 選項先 json.dumps 成 JSON 字串，再包一層：模板裡是
    # OPTS = json.loads(__JOB_OPTIONS_JSON__)，佔位符要是一個
    # JSON 字串字面量（不是直接放物件字面量，避免 true/false/null
    # 在 Python 裡不合法）。
    return (template
            .replace("__USER_PROMPT_JSON__",
                     json.dumps(prompt, ensure_ascii=True))
            .replace("__JOB_OPTIONS_JSON__",
                     json.dumps(json.dumps(opts, ensure_ascii=True),
                                ensure_ascii=True)))


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


def download_outputs(job_id, settings, output, kernel_owner):
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
        zip_bytes = download_zip(settings, kernel_owner)
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
    """跑一個任務：提交 -> 輪詢 -> 下載。任何 401 都只記金鑰無效。

    用任務建立時記下的伺服器跑（不是當下切換到的那個），
    那個伺服器刪了才退回目前伺服器。內核的擁有者就是這個
    伺服器的 Kaggle 用戶名；有配數據集就掛上（不用再下載模型），
    沒配就走下載老路。"""
    job_id = job["id"]
    settings = load_settings()
    server = find_server_by_name(settings, job.get("server")) \
        or current_server(settings)
    if not server or not server.get("username") or not server.get("key"):
        fail_job(job_id, NO_KEY_TEXT)
        return
    kernel_owner = server["username"]
    kernel_ref = f"{kernel_owner}/{KERNEL_SLUG}"
    credentials = {"username": server["username"], "key": server["key"]}
    try:
        script = build_kernel_script(job["prompt"], job.get("options"))
        # 排隊時被取消就直接停，不要提交到 Kaggle。
        if get_job_status(job_id) != "queued":
            return
        body = {
            "slug": kernel_ref,
            "newTitle": KERNEL_SLUG,
            "text": script,
            "language": "python",
            "kernelType": "script",
            "isPrivate": True,
            "enableGpu": True,
            "enableTpu": False,
            "enableInternet": True,
            "datasetDataSources": [server["dataset"]]
            if server.get("dataset") else [],
            "kernelDataSources": [],
            "competitionDataSources": [],
            "modelDataSources": [],
            "categoryIds": [],
            "sessionTimeoutSeconds": 7200,
            "machineShape": "NvidiaTeslaT4",
        }
        resp = kaggle_post(credentials, "/kernels.KernelsApiService/SaveKernel",
                           body, timeout=120)
        version = resp.get("versionNumber") if isinstance(resp, dict) else None
        # 只有還是 queued 才改成 running；改了 0 行就是剛被取消，直接停。
        if not try_start_job(job_id, version):
            return
        status = None
        deadline = time.time() + MAX_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            # 輪詢時被取消就直接停：不要標成失敗，也不要去下載輸出。
            # Kaggle 內核本身會自己超時，與工作台無關。
            if get_job_status(job_id) == "cancelled":
                return
            st = kaggle_post(
                credentials, "/kernels.KernelsApiService/GetKernelSessionStatus",
                {"userName": kernel_owner, "kernelSlug": KERNEL_SLUG}, timeout=60)
            status = (st or {}).get("status") if isinstance(st, dict) else None
            if status in ("COMPLETE", "ERROR", "FAILED", "CANCELLED"):
                break
            # 狀態為空（剛提交還沒分到機器，v4 時空了約 17 分鐘）就繼續等。
        if status != "COMPLETE":
            if status in ("ERROR", "FAILED", "CANCELLED"):
                try:
                    out = kaggle_post(
                        credentials,
                        "/kernels.KernelsApiService/ListKernelSessionOutput",
                        {"userName": kernel_owner, "kernelSlug": KERNEL_SLUG,
                         "pageSize": 20}, timeout=60)
                    err = extract_error_log(out)
                except Exception:
                    err = ""
                fail_job(job_id, err or "Kaggle 執行失敗，請看設定頁的金鑰是否正確")
            else:
                fail_job(job_id, "等待超時（2 小時），任務未完成")
            return
        out = kaggle_post(
            credentials, "/kernels.KernelsApiService/ListKernelSessionOutput",
            {"userName": kernel_owner, "kernelSlug": KERNEL_SLUG,
             "pageSize": 20}, timeout=60)
        summary = download_outputs(job_id, credentials, out, kernel_owner)
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


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """所有 /api/* 都要登入才給過；只放行 /api/health 與 /api/auth/*。
    靜態頁面本身不擋（裡面沒有資料），資料都在 API 後面。"""
    path = request.url.path
    if path.startswith("/api/") and path != "/api/health" \
            and not path.startswith("/api/auth/"):
        err = _require_auth(request)
        if err is not None:
            return err
    return await call_next(request)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """靜態頁面不給瀏覽器快取：上次升級後，舊版 index.html/app.js 被瀏覽器
    快取了 4 小時，新版一直出不來。加了這個頭，以後每次打開都是最新版。"""
    resp = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and (
            path == "/" or path.endswith((".html", ".js", ".css"))):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- 帳號密碼端點
@app.get("/api/auth/status")
def auth_status(request: Request):
    authenticated, username = _auth_state(request)
    return {"authenticated": authenticated, "username": username}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """帳號密碼登入：對了發 24 小時登入憑證；不對只回「帳號或密碼不對」。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "帳號或密碼不對"}, status_code=400)
    username = str(data.get("username") or "").strip()
    password = data.get("password") or ""
    auth = load_auth()
    if username != auth["username"] or not _verify_password(auth, password):
        return JSONResponse({"error": "帳號或密碼不對"}, status_code=400)
    resp = JSONResponse({"ok": True, "username": auth["username"]})
    return _set_session_cookie(resp, auth)


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    return _clear_auth_cookies(resp)


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    """改密碼：必須已登入；先驗目前的密碼，再驗新密碼至少 4 位。
    成功後換新鹽、新雜湊、新 session_secret，舊登入憑證立刻失效，
    回前端請使用者用新密碼重新登入。"""
    authenticated, _ = _auth_state(request)
    if not authenticated:
        return JSONResponse({"error": NOT_LOGGED_IN_TEXT}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    current = data.get("current_password") or ""
    new = data.get("new_password") or ""
    auth = load_auth()
    if not _verify_password(auth, current):
        return JSONResponse({"error": "目前的密碼不對"}, status_code=400)
    if len(new) < MIN_PASSWORD_LENGTH:
        return JSONResponse(
            {"error": f"新密碼至少要 {MIN_PASSWORD_LENGTH} 位"}, status_code=400)
    salt = secrets.token_hex(16)
    auth["password_hash"] = _hash_password(new, salt)
    auth["salt"] = salt
    auth["session_secret"] = secrets.token_hex(32)
    save_auth(auth)
    resp = JSONResponse({"ok": True})
    return _clear_auth_cookies(resp)


@app.post("/api/jobs")
async def create_job_endpoint(request: Request):
    """純文字生圖：只收提示詞 + 面板選項（尺寸、比例、種子、步數、CFG）。
    不傳選項就沿用已驗證的 v4 配方（768x768、20 步、CFG 1.0、種子 42）。
    參考模式請走 /api/jobs/with-refs。"""
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
    raw = dict(data) if isinstance(data, dict) else {}
    if (raw.get("mode") or "t2i") != "t2i":
        return JSONResponse(
            {"error": "參考生圖請用帶參考圖的提交方式"}, status_code=400)
    server = current_server()
    raw["use_dataset"] = bool(server and server.get("dataset"))
    options = _normalize_options(raw)
    job_id = create_job(prompt, options, server=server["name"] if server else "")
    return {"id": job_id, "status": "queued"}


def _process_ref_file(data, index, filename, content_type):
    """檢查一張參考圖，轉成 JPEG、長邊縮到 1024 像素，回傳處理好的位元組。

    只接受 jpg/png/webp，每張不超過 10MB；壞掉的圖、不是圖的檔案都拒絕。"""
    if len(data) > MAX_REF_SIZE_BYTES:
        return None, f"第 {index} 張圖超過 {MAX_REF_SIZE_MB}MB，請換張小一點的圖"
    if content_type and content_type.lower() not in REF_CONTENT_TYPES:
        ext = Path(filename or "").suffix.lower()
        if ext not in REF_EXTENSIONS:
            return None, f"第 {index} 張圖格式不對，只接受 jpg、png、webp"
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:
        return None, f"第 {index} 張圖打不開，請確認是正常的圖片檔案"
    if img.format not in REF_IMAGE_FORMATS:
        return None, f"第 {index} 張圖格式不對，只接受 jpg、png、webp"
    img = img.convert("RGB")
    img.thumbnail((MAX_REF_EDGE, MAX_REF_EDGE), Image.LANCZOS)
    out = BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue(), None


@app.post("/api/jobs/with-refs")
async def create_job_with_refs(request: Request):
    """參考生圖：一次收表單欄位（提示詞、模式、比例、寬、高、種子、
    步數、CFG、參考強度）+ 圖片檔案。

    圖存在 /data/refs/<任務id>/（任務 id 是隨機的，猜不到，相當於口令），
    再用公開路由 /refs/{任務id}/{檔名} 提供給 Kaggle 內核下載。
    單圖參考只能傳 1 張，多圖參考最多 4 張。"""
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "上傳內容讀取失敗"}, status_code=400)
    prompt = (form.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "提示詞不能為空"}, status_code=400)
    if len(prompt) > 2000:
        return JSONResponse({"error": "提示詞太長，請控制在 2000 字以內"},
                            status_code=400)
    mode = (form.get("mode") or "").strip()
    if mode not in ("single", "multi"):
        return JSONResponse(
            {"error": "請選擇單圖參考或多圖參考"}, status_code=400)
    files = form.getlist("refs")
    if mode == "single" and len(files) != 1:
        return JSONResponse({"error": "單圖參考只能上傳 1 張圖"},
                            status_code=400)
    if mode == "multi" and not (1 <= len(files) <= MAX_REFS):
        return JSONResponse(
            {"error": f"多圖參考最多上傳 {MAX_REFS} 張圖"}, status_code=400)
    processed = []
    for i, f in enumerate(files, 1):
        data = await f.read()
        blob, error = _process_ref_file(
            data, i, f.filename or "", f.content_type or "")
        if error:
            return JSONResponse({"error": error}, status_code=400)
        processed.append(blob)
    job_id = uuid.uuid4().hex[:12]
    ref_dir = REFS_DIR / job_id
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
        for i, blob in enumerate(processed, 1):
            (ref_dir / f"ref_{i}.jpg").write_bytes(blob)
    except OSError:
        shutil.rmtree(ref_dir, ignore_errors=True)
        return JSONResponse({"error": "參考圖保存失敗，請再試一次"},
                            status_code=500)
    base = _public_base_url(request)
    ref_urls = [f"{base}/refs/{job_id}/ref_{i}.jpg"
                for i in range(1, len(processed) + 1)]
    raw = {k: v for k, v in form.items() if k != "refs"}
    server = current_server()
    raw["use_dataset"] = bool(server and server.get("dataset"))
    options = _normalize_options(raw, ref_count=len(processed),
                                 ref_urls=ref_urls)
    create_job(prompt, options, job_id=job_id,
               server=server["name"] if server else "")
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


@app.get("/refs/{job_id}/{filename}")
def get_ref(job_id: str, filename: str):
    """公開路由：給 Kaggle 內核腳本下載參考圖，不鑑權
    （內核那邊沒有登入憑證）。

    任務 id 是隨機的 12 位十六進位，相當於口令，猜不到；
    檔名只允許 ref_1.jpg 這種格式，/ 與 .. 一律擋掉，防目錄穿越。"""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return JSONResponse({"error": "找不到這個檔案"}, status_code=404)
    if not re.fullmatch(r"ref_[0-9]+\.jpg", filename):
        return JSONResponse({"error": "找不到這個檔案"}, status_code=404)
    base = (REFS_DIR / job_id).resolve()
    try:
        path = (base / filename).resolve()
    except (OSError, RuntimeError):
        return JSONResponse({"error": "找不到這個檔案"}, status_code=404)
    if path.parent != base or not path.is_file():
        return JSONResponse({"error": "找不到這個檔案"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg", filename=filename)


# 刪任務的檔案：{id}.png / {id}.hardware.json / {id}.summary.json
# 加上這個任務的參考圖目錄 /data/refs/{id}/。
# 只刪這個任務自己的檔案，絕不碰別的目錄。
_JOB_FILE_SUFFIXES = (".png", ".hardware.json", ".summary.json")


def _delete_job_files(job_id):
    for suffix in _JOB_FILE_SUFFIXES:
        try:
            (JOBS_DIR / f"{job_id}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass
    try:
        shutil.rmtree(REFS_DIR / job_id, ignore_errors=True)
    except OSError:
        pass


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job_endpoint(job_id: str):
    """取消一個排隊中或進行中的任務。
    工作台不再等它、不再跑它；已經在 Kaggle 上跑的內核本身
    會跑到它自己的超時才停（Kaggle 沒有停止內核的 API），
    介面上的文字要誠實寫清楚這一點。"""
    with DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            conn.close()
            return JSONResponse({"error": "找不到這個任務"}, status_code=404)
        if row["status"] not in ("queued", "running"):
            conn.close()
            return JSONResponse({"error": "任務已結束，不能取消"},
                                status_code=400)
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ?, "
            "error = '已取消' WHERE id = ?", (_now(), job_id))
        conn.commit()
        conn.close()
    _delete_job_files(job_id)
    return {"ok": True, "status": "cancelled"}


@app.delete("/api/jobs/{job_id}")
def delete_job_endpoint(job_id: str):
    """刪一條已結束的任務（資料庫記錄 + 圖片檔案）。
    排隊中或進行中的任務不給刪。"""
    with DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            conn.close()
            return JSONResponse({"error": "找不到這個任務"}, status_code=404)
        if row["status"] in ("queued", "running"):
            conn.close()
            return JSONResponse({"error": "任務進行中，不能刪"}, status_code=400)
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()
    _delete_job_files(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_jobs_endpoint():
    """清空全部已結束的任務（資料庫記錄 + 圖片檔案），
    排隊中或進行中的任務不動。已取消的也算已結束，一起清。"""
    with DB_LOCK:
        conn = _conn()
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN "
            "('completed', 'failed', 'cancelled')"
            ).fetchall()
        ids = [r["id"] for r in rows]
        conn.execute(
            "DELETE FROM jobs WHERE status IN "
            "('completed', 'failed', 'cancelled')")
        conn.commit()
        conn.close()
    for job_id in ids:
        _delete_job_files(job_id)
    return {"ok": True, "deleted": len(ids)}


@app.get("/api/quota")
def quota():
    """目前伺服器的 GPU 額度用量：只算這個伺服器跑過的任務，
    別的伺服器各有自己的額度，不混在一起。"""
    server = current_server()
    if not server:
        return {"used_seconds": 0, "total_seconds": WEEKLY_QUOTA_SECONDS,
                "remaining_seconds": WEEKLY_QUOTA_SECONDS}
    with DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(gpu_seconds), 0) FROM jobs "
            "WHERE status = 'completed' AND server = ?",
            (server["name"],)).fetchone()
        conn.close()
    used = float(row[0] or 0)
    return {"used_seconds": used, "total_seconds": WEEKLY_QUOTA_SECONDS,
            "remaining_seconds": max(0, WEEKLY_QUOTA_SECONDS - used)}


_KAGGLE_REACH_CACHE = {"ts": 0.0, "reachable": False, "error": "",
                       "server_id": None}


def _kaggle_reachable():
    """試連 Kaggle 一次，結果快取 60 秒。回傳 (通不通, 原因)。
    快取按伺服器分開：切換伺服器後重新試連，不拿上一個伺服器的結果充數。"""
    server = current_server()
    if not server or not server.get("username") or not server.get("key"):
        return False, "未設定金鑰"
    now = time.time()
    if now - _KAGGLE_REACH_CACHE["ts"] < 60 \
            and _KAGGLE_REACH_CACHE.get("server_id") == server["id"]:
        return (_KAGGLE_REACH_CACHE["reachable"],
                _KAGGLE_REACH_CACHE["error"])
    credentials = {"username": server["username"], "key": server["key"]}
    try:
        kaggle_get(credentials, "/kernels/list?user=" +
                   urllib.parse.quote(server["username"], safe=""),
                   timeout=15)
        reachable, error = True, ""
    except KaggleAuthError:
        reachable, error = False, "金鑰無效或過期"
    except KaggleError as e:
        reachable, error = False, str(e)[:100]
    _KAGGLE_REACH_CACHE.update(
        {"ts": now, "reachable": reachable, "error": error,
         "server_id": server["id"]})
    return reachable, error


@app.get("/api/status")
def server_status():
    """伺服器運行狀態：運行時間、目前伺服器、Kaggle 連線、
    目前伺服器的額度、任務統計、進行中與最近完成的任務。"""
    server = current_server()
    reachable, kaggle_error = _kaggle_reachable()
    with DB_LOCK:
        conn = _conn()
        counts = {}
        for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
            counts[row["status"]] = row["n"]
        running = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('running', 'queued') "
            "ORDER BY CASE WHEN status = 'running' THEN 0 ELSE 1 END, "
            "created_at ASC LIMIT 1").fetchone()
        last = conn.execute(
            "SELECT * FROM jobs WHERE finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1").fetchone()
        if server:
            used_row = conn.execute(
                "SELECT COALESCE(SUM(gpu_seconds), 0) FROM jobs "
                "WHERE status = 'completed' AND server = ?",
                (server["name"],)).fetchone()
        else:
            used_row = (0,)
        conn.close()
    used = float(used_row[0] or 0)
    return {
        "uptime_seconds": int(time.time() - START_TIME),
        "server": public_server(server) if server else None,
        "kaggle_configured": bool(server and server.get("username")
                                  and server.get("key")),
        "kaggle_reachable": reachable,
        "kaggle_error": kaggle_error,
        "quota": {"used_seconds": used, "total_seconds": WEEKLY_QUOTA_SECONDS,
                  "remaining_seconds": max(0, WEEKLY_QUOTA_SECONDS - used)},
        "counts": {"queued": counts.get("queued", 0),
                   "running": counts.get("running", 0),
                   "completed": counts.get("completed", 0),
                   "failed": counts.get("failed", 0)},
        "current_job": (_row_to_job(running) if running else None),
        "last_finished_job": (_row_to_job(last) if last else None),
    }


@app.get("/api/settings")
def get_settings():
    """設定頁要的資料：目前伺服器的用戶名、金鑰狀態（只回後 4 位）、
    全部伺服器清單（也不含完整金鑰）。
    kaggle_username / key_saved / key_hint 保留下來，是給目前
    伺服器那張 Kaggle 卡片用的舊欄位。"""
    settings = load_settings()
    server = current_server(settings)
    result = {
        "kaggle_configured": bool(server and server.get("username")
                                  and server.get("key")),
        "current_server_id": server["id"] if server else None,
        "servers": [public_server(s)
                    for s in settings.get("servers") or []],
        "kaggle_username": server["username"] if server else "",
        "key_saved": bool(server and server.get("key")),
        "key_hint": _key_hint(server.get("key")) if server else "",
    }
    return result


@app.post("/api/settings")
async def save_settings_endpoint(request: Request):
    """改目前伺服器的用戶名和金鑰（設定頁 Kaggle 卡片用的入口）：
    金鑰留空表示沿用已保存的金鑰，用戶名留空表示沿用原來的
    用戶名；兩個都空才回 400。絕不回傳完整金鑰。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    username = (data.get("username") or "").strip() if isinstance(data, dict) else ""
    key = (data.get("key") or "").strip() if isinstance(data, dict) else ""
    if not username and not key:
        return JSONResponse({"error": "沒有要改的內容，用戶名和金鑰都留空了"},
                            status_code=400)
    settings = load_settings()
    server = current_server(settings)
    if server is None:
        return JSONResponse(
            {"error": "還沒有伺服器，請先在「伺服器」區新增一個"},
            status_code=400)
    if username:
        server["username"] = username
    if key:
        server["key"] = key
    if not server["username"] or not server["key"]:
        return JSONResponse(
            {"error": "用戶名和金鑰都要有，現在還缺一項，請填上"},
            status_code=400)
    save_settings_full(settings)
    return {"ok": True, "key_saved": bool(server["key"]),
            "key_changed": bool(key)}


@app.post("/api/settings/test")
def test_settings():
    """測試目前伺服器的連線（設定頁「測試連線」按鈕）。"""
    server = current_server()
    if not server or not server.get("username") or not server.get("key"):
        return {"ok": False, "error": NO_KEY_TEXT}
    credentials = {"username": server["username"], "key": server["key"]}
    try:
        kaggle_get(credentials, "/kernels/list?user=" +
                   urllib.parse.quote(server["username"], safe=""), timeout=30)
        return {"ok": True}
    except KaggleAuthError:
        return {"ok": False, "error": KEY_ERROR_TEXT}
    except KaggleError as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------- 伺服器管理
def _valid_dataset_ref(dataset):
    """數據集填了就要是 owner/slug 的樣子（例如
    xiongyong5207/qwen-image-21-models），留空表示不配。"""
    if not dataset:
        return True
    parts = dataset.split("/")
    return len(parts) == 2 and all(p.strip() for p in parts)


@app.get("/api/servers")
def list_servers():
    """伺服器清單（右上角切換器與設定頁「伺服器」區共用），
    絕不含完整金鑰。"""
    settings = load_settings()
    server = current_server(settings)
    return {"current_server_id": server["id"] if server else None,
            "servers": [public_server(s)
                        for s in settings.get("servers") or []]}


@app.post("/api/servers")
async def add_server(request: Request):
    """新增一個伺服器：名字、用戶名、金鑰都要填；數據集可留空
    （留空就是每次跑圖都重新下載模型）。名字不能重複。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    name = str(data.get("name") or "").strip()
    username = str(data.get("username") or "").strip()
    key = str(data.get("key") or "").strip()
    dataset = str(data.get("dataset") or "").strip()
    if not name:
        return JSONResponse({"error": "請給伺服器起個名字"}, status_code=400)
    if not username or not key:
        return JSONResponse({"error": "用戶名和金鑰都要填"}, status_code=400)
    if not _valid_dataset_ref(dataset):
        return JSONResponse(
            {"error": "數據集請填 owner/slug 的格式，"
                      "例如 xiongyong5207/qwen-image-21-models"},
            status_code=400)
    settings = load_settings()
    if any(s["name"] == name for s in settings.get("servers") or []):
        return JSONResponse({"error": "已經有一個同名的伺服器了，請換個名字"},
                            status_code=400)
    srv = _new_server(name, username, key, dataset)
    settings["servers"].append(srv)
    if not settings.get("current_server_id"):
        settings["current_server_id"] = srv["id"]
    save_settings_full(settings)
    return {"ok": True, "server": public_server(srv)}


@app.put("/api/servers/{server_id}")
async def update_server(server_id: str, request: Request):
    """改一個伺服器：名字、用戶名、數據集都能改；金鑰留空表示沿用原值。
    名字改了也不能跟別的伺服器重複。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "請求內容不是合法的 JSON"}, status_code=400)
    settings = load_settings()
    srv = find_server(settings, server_id)
    if not srv:
        return JSONResponse({"error": "找不到這個伺服器"}, status_code=404)
    name = str(data.get("name") or "").strip()
    username = str(data.get("username") or "").strip()
    key = str(data.get("key") or "").strip()
    dataset = str(data.get("dataset") or "").strip()
    if name and name != srv["name"] and any(
            s["name"] == name for s in settings.get("servers") or []):
        return JSONResponse({"error": "已經有一個同名的伺服器了，請換個名字"},
                            status_code=400)
    if not _valid_dataset_ref(dataset):
        return JSONResponse(
            {"error": "數據集請填 owner/slug 的格式，"
                      "例如 xiongyong5207/qwen-image-21-models"},
            status_code=400)
    if name:
        srv["name"] = name
    if username:
        srv["username"] = username
    if key:
        srv["key"] = key
    if "dataset" in data:
        srv["dataset"] = dataset
    if not srv["username"] or not srv["key"]:
        return JSONResponse({"error": "用戶名和金鑰都要有，現在還缺一項"},
                            status_code=400)
    save_settings_full(settings)
    return {"ok": True, "server": public_server(srv)}


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: str):
    """刪一個伺服器：至少要保留一個。刪掉的是目前伺服器，
    就把目前切到剩下的第一個。已跑過的任務記錄不受影響，
    只是以後新任務不會再用這個伺服器跑。"""
    settings = load_settings()
    if len(settings.get("servers") or []) <= 1:
        return JSONResponse({"error": "至少要保留一個伺服器，不能全刪掉"},
                            status_code=400)
    srv = find_server(settings, server_id)
    if not srv:
        return JSONResponse({"error": "找不到這個伺服器"}, status_code=404)
    settings["servers"] = [s for s in settings["servers"]
                           if s["id"] != server_id]
    if settings.get("current_server_id") == server_id:
        settings["current_server_id"] = settings["servers"][0]["id"]
    save_settings_full(settings)
    return {"ok": True}


@app.post("/api/servers/{server_id}/activate")
def activate_server(server_id: str):
    """把某個伺服器設為目前伺服器：之後的新任務都用它提交，
    額度與狀態顯示也跟著換。"""
    settings = load_settings()
    srv = find_server(settings, server_id)
    if not srv:
        return JSONResponse({"error": "找不到這個伺服器"}, status_code=404)
    settings["current_server_id"] = server_id
    save_settings_full(settings)
    return {"ok": True, "server": public_server(srv)}


@app.api_route("/api/{rest:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_unknown(rest: str):
    """未知的 /api/* 路徑：一律回 JSON 404，不掉進靜態檔案處理
    （靜態檔案只認 GET，POST 會回 405，容易誤會接口還在）。"""
    return JSONResponse({"error": "找不到這個接口"}, status_code=404)


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
