#!/usr/bin/env python3
"""Qwen-Image-2.1 GGUF minimal test on Kaggle GPU T4 x2.

One thing only: install ComfyUI + ComfyUI-GGUF, download the 3 GGUF/INT8
files to /kaggle/tmp (scratch, NOT /kaggle/working), generate 1 neutral
test image via the ComfyUI API, save the PNG to /kaggle/working.

Prompt is neutral on purpose: a small computer shop storefront in daytime.
No adult / sexy prompt. No BF16 download. No second quantisation.

Files (exact sizes from the Hugging Face API, 2026-11-08):
  1. unsloth/Qwen-Image-2.1-GGUF / qwen-image-2.1-Q4_K_M.gguf
     4,199,565,024 bytes = 4.20 GB, denoiser only
  2. Comfy-Org/Qwen-Image-2.1 / text_encoders/qwen3vl_8b_int8_convrot.safetensors
     9,350,798,360 bytes = 9.35 GB, text encoder, CPU offload
  3. Comfy-Org/Qwen-Image-2.1 / vae/qwen_image_2.1_vae_bf16.safetensors
     675,509,688 bytes = 0.68 GB
  Total: 14,225,873,072 bytes = 14.23 GB to scratch.
  NOTE: the 11.19 GB figure in the brief mixes a GGUF denoiser with a
  safetensors W4A8 encoder from a different runtime. Do not mix them.
  For ComfyUI-GGUF the correct set is the 3 files above (14.23 GB).
  For stable-diffusion.cpp the correct set is GGUF denoiser 4.20 GB +
  GGUF encoder UD-Q4_K_XL 5.15 GB + VAE 0.68 GB = 10.03 GB.

Run with: kaggle kernels push -p <folder> --accelerator NvidiaTeslaT4 -t 7200
Then:    kaggle kernels status xiongyong5207/qwen-image-21-gguf-t4-test
         kaggle kernels output xiongyong5207/qwen-image-21-gguf-t4-test -p <out>

v2.4: 支援參考生圖（官方 Qwen-Image-2.1 編輯工作流）與面板選項
（尺寸、比例、種子、步數、CFG、參考強度）。提示詞與選項由工作台後端
注入模板裡的兩個佔位符，工作流由 build_workflow() 按選項拼出；
純文字模式仍走原來的 v4 工作流。
v2.5: 數據集模式——內核提交時掛上 Kaggle 數據集
（xiongyong5207/qwen-image-21-models），模型檔直接從掛載目錄讀，
不用再下載 14.23GB。ComfyUI 用 extra_model_paths.yaml 指到掛載
目錄（官方機制），工作流本身一個節點都不用改；沒配數據集的
伺服器走老路（下載）。掛載路徑不寫死，按檔名遞迴找。
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

START = time.time()
WORK = "/kaggle/working"
TMP = "/kaggle/tmp/qwen21"
COMFY = "/kaggle/tmp/ComfyUI"


def log(msg):
    print(f"[{time.time() - START:7.1f}s] {msg}", flush=True)


def run(cmd, **kw):
    log(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise SystemExit(f"FAILED rc={r.returncode}: {' '.join(cmd)}")
    return r


def run_optional(cmd, **kw):
    """Diagnostic-only command: never kills the script. v2 fix for the
    v1 crash where a missing nvidia-smi (FileNotFoundError) at 0.77s
    killed the whole test before anything was installed."""
    log(f"$ {' '.join(cmd)} (optional, failure is only logged)")
    try:
        r = subprocess.run(cmd, **kw)
        if r.returncode != 0:
            log(f"OPTIONAL FAILED rc={r.returncode}: {' '.join(cmd)}")
        return r
    except FileNotFoundError:
        log(f"OPTIONAL NOT FOUND, skipping: {cmd[0]}")
        return None
    except Exception as e:
        log(f"OPTIONAL ERROR {type(e).__name__}: {e} for {' '.join(cmd)}")
        return None


def http_get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


# 生圖選項由工作台後端注入（見 app/main.py build_kernel_script）。
# 選項佔位符是 JSON 字串，json.loads 還原成 dict。
OPTS = json.loads(__JOB_OPTIONS_JSON__)
PROMPT = __USER_PROMPT_JSON__


def build_workflow(opts, prompt):
    """按選項拼 ComfyUI API 格式工作流（純函式，不碰磁碟、不碰網路）。

    文生圖（mode=t2i）：沿用已驗證的 v4 工作流，只把寬、高、步數、
    CFG、種子換成選項裡的值，節點與模型檔案一點不變。
    參考生圖（mode=single/multi）：官方 Qwen-Image-2.1 編輯工作流
    （Comfy-Org 官方範本 image_qwen_image_2_1_image_edit，API 格式見
    NewGwen2.1 的 qwen_image_2.1_gguf_edit_api.json）：
    LoadImage -> TextEncodeQwenImage21（images.image_N + VAE），
    image_1 的 latent 決定畫布；QwenImage21Cache 接在模型與採樣器之間；
    提示詞裡用 <image1>、<image2> 指第幾張參考圖。
    官方工作流沒有「參考強度」這個參數，這裡用採樣器的 denoise 實現：
    denoise = 1 - 強度，強度越大越像原圖（預設 0.7 -> denoise 0.3）。
    出圖檔名固定是 qwen_test_chufeng，後端按這個名字取圖，兩種模式一樣。"""
    mode = str(opts.get("mode") or "t2i").strip()
    if mode == "text":
        mode = "t2i"  # 舊寫法，當成文生圖。
    if mode not in ("t2i", "single", "multi"):
        # 絕不默默掉進參考分支：之前有呼叫方傳了 "text" 卻拿到
        # 參考工作流（denoise 0.3、沒有參考圖），結果會是一張糊圖。
        raise ValueError(f"未知的生圖模式: {mode!r}")
    width = int(opts.get("width") or 768)
    height = int(opts.get("height") or 768)
    steps = int(opts.get("steps") or 20)
    cfg = float(opts.get("cfg") or 1.0)
    seed = int(opts.get("seed") if opts.get("seed") is not None else 42)
    if mode == "t2i":
        return {
            "1": {"class_type": "UnetLoaderGGUF",
                  "inputs": {"unet_name": "qwen-image-2.1-Q4_K_M.gguf"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                             "type": "qwen_image"}},
            "3": {"class_type": "VAELoader",
                  "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
            "4": {"class_type": "CLIPTextEncode",
                  "inputs": {"clip": ["2", 0], "text": prompt}},
            "9": {"class_type": "CLIPTextEncode",
                  "inputs": {"clip": ["2", 0],
                             "text": "blurry, low quality, distorted"}},
            "5": {"class_type": "EmptyLatentImage",
                  "inputs": {"batch_size": 1, "height": height, "width": width}},
            "6": {"class_type": "KSampler",
                  "inputs": {"cfg": cfg, "denoise": 1.0, "latent_image": ["5", 0],
                             "model": ["1", 0], "negative": ["9", 0],
                             "positive": ["4", 0], "sampler_name": "euler",
                             "scheduler": "simple", "seed": seed, "steps": steps}},
            "7": {"class_type": "VAEDecode",
                  "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
            "8": {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": "qwen_test_chufeng",
                             "images": ["7", 0]}},
        }
    # 參考生圖：官方編輯工作流（見上方說明）。
    # ref_count 優先用 opts 的值；沒給就從 refs 清單推（呼叫方可能
    # 只傳 refs）。單圖必須恰好 1 張，多圖 1–4 張；張數不對直接
    # 報錯，絕不拼一個沒有 LoadImage 的「參考」工作流——那會用
    # denoise 0.3 在全新 latent 上採樣，出糊圖。
    ref_count = int(opts.get("ref_count") or 0)
    if ref_count <= 0 and isinstance(opts.get("refs"), list):
        ref_count = len(opts["refs"])
    if mode == "single" and ref_count != 1:
        raise ValueError(f"單圖參考必須恰好 1 張參考圖，拿到 {ref_count} 張")
    if mode == "multi" and not (1 <= ref_count <= 4):
        raise ValueError(f"多圖參考必須是 1–4 張參考圖，拿到 {ref_count} 張")
    strength = float(opts.get("ref_strength", 0.7))
    denoise = round(1.0 - max(0.0, min(1.0, strength)), 4)
    encode_inputs = {
        "clip": ["2", 0],
        "vae": ["3", 0],
        "prompt": prompt,
        "negative_prompt": "",
        "resolution": 0,
    }
    workflow = {
        "1": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "qwen-image-2.1-Q4_K_M.gguf"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                         "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": encode_inputs},
        "9": {"class_type": "QwenImage21Cache",
              "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["9", 0], "positive": ["4", 0],
                         "negative": ["4", 1], "latent_image": ["4", 2],
                         "seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": "euler", "scheduler": "simple",
                         "denoise": denoise}},
        "7": {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "qwen_test_chufeng",
                         "images": ["7", 0]}},
    }
    for i in range(1, ref_count + 1):
        node_id = str(9 + i)
        workflow[node_id] = {"class_type": "LoadImage",
                             "inputs": {"image": f"ref_{i}.jpg"}}
        encode_inputs[f"images.image_{i}"] = [node_id, 0]
    return workflow


# 數據集模式用的小工具（v2.5）。放在這裡是為了讓測試能單獨抽出來測，
# 不要把這些邏輯寫進第 3 步的主流程裡。
DATASET_SEARCH_ROOT = "/kaggle/input"


def find_dataset_file(name, expected_size, root=DATASET_SEARCH_ROOT):
    """在掛載的數據集裡按檔名找一個模型檔（遞迴找），並核對大小。
    找不到、大小不對就直接報錯停掉，不要默默掉回下載：那會燒掉
    一次下載的時間，還讓人以為用了緩存。"""
    hits = glob.glob(os.path.join(root, "**", name), recursive=True)
    if not hits:
        raise SystemExit(
            f"DATASET MISSING {name}：數據集裡找不到這個檔，"
            "請檢查伺服器的數據集設定")
    path = hits[0]
    got = os.path.getsize(path)
    if got != expected_size:
        raise SystemExit(
            f"DATASET SIZE MISMATCH {name}：got {got}，expected {expected_size}")
    return path


def write_extra_model_paths(comfy_root, category_paths):
    """寫 ComfyUI 的 extra_model_paths.yaml，讓三類模型指向數據集
    掛載目錄。category_paths 是 {類別: 檔案完整路徑}，
    類別是 diffusion_models / text_encoders / vae。
    ComfyUI 啟動時會讀這個檔案（官方機制），工作流不用改。"""
    lines = ["comfyui:"]
    for category in ("diffusion_models", "text_encoders", "vae"):
        lines.append(
            f"    {category}: {os.path.dirname(category_paths[category])}")
    yaml_path = os.path.join(comfy_root, "extra_model_paths.yaml")
    with open(yaml_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return yaml_path


# 1. Hardware check (v2: tolerant). Print the real GPU, RAM, disk.
# Do not assume T4 x2, and NEVER exit here: a diagnostic failure must
# not kill the test. Record everything into hardware.json in /kaggle/working
# so the result is visible even if a later step fails.
log("== 1. hardware (v2 tolerant) ==")
hw = {"version": "v2", "path": os.environ.get("PATH", ""),
      "nvidia_smi_which": shutil.which("nvidia-smi"),
      "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
      "kaggle_run_type": os.environ.get("KAGGLE_KERNEL_RUN_TYPE")}
log(f"PATH={hw['path']}")
log(f"which nvidia-smi: {hw['nvidia_smi_which']}")
for cand in ["/usr/local/nvidia/bin/nvidia-smi", "/usr/local/cuda/bin/nvidia-smi",
             "/usr/bin/nvidia-smi"]:
    log(f"candidate {cand} exists={os.path.exists(cand)}")
log(f"ls /usr/local/nvidia/bin: {glob.glob('/usr/local/nvidia/bin/*')[:10]}")
log(f"ls /usr/bin/nvidia*: {glob.glob('/usr/bin/nvidia*')[:10]}")
log(f"/proc/driver/nvidia exists={os.path.exists('/proc/driver/nvidia')}")
try:
    import torch
    hw["torch_cuda_available"] = torch.cuda.is_available()
    hw["torch_cuda_device_count"] = torch.cuda.device_count()
    hw["torch_cuda_device_name"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    log(f"torch.cuda.is_available={hw['torch_cuda_available']} count={hw['torch_cuda_device_count']} name={hw['torch_cuda_device_name']}")
except Exception as e:
    hw["torch_error"] = f"{type(e).__name__}: {e}"
    log(f"torch check failed (non-fatal): {hw['torch_error']}")
# nvidia-smi itself is optional now: try it, log, move on either way.
run_optional(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv"])
run_optional(["free", "-h"])
run_optional(["df", "-h", "/kaggle/working", "/kaggle/tmp"])
log(f"python {sys.version.split()[0]} work={WORK} tmp={TMP}")
os.makedirs(WORK, exist_ok=True)
with open(os.path.join(WORK, "hardware.json"), "w") as f:
    json.dump(hw, f, indent=2)
log(f"hardware summary written to {WORK}/hardware.json: {hw}")

# 2. Install ComfyUI + ComfyUI-GGUF into scratch.
log("== 2. install ComfyUI ==")
os.makedirs(TMP, exist_ok=True)
if not os.path.isdir(COMFY):
    run(["git", "clone", "--depth", "1",
         "https://github.com/comfyanonymous/ComfyUI.git", COMFY])
run([sys.executable, "-m", "pip", "install", "-q", "-r",
     os.path.join(COMFY, "requirements.txt")])
gguf_node = os.path.join(COMFY, "custom_nodes", "ComfyUI-GGUF")
if not os.path.isdir(gguf_node):
    run(["git", "clone", "--depth", "1",
         "https://github.com/leejet/ComfyUI-GGUF.git", gguf_node])
run([sys.executable, "-m", "pip", "install", "-q", "gguf", "aria2"])
log("ComfyUI + ComfyUI-GGUF installed")

# 3. 模型：有數據集就用掛載的，沒數據集就下載（老路）。
#    數據集模式（v2.5）：內核提交時掛上 Kaggle 數據集，模型檔就在
#    /kaggle/input 底下，不用再下載 14.23GB。ComfyUI 用
#    extra_model_paths.yaml 指到掛載目錄（官方機制），工作流本身
#    一個節點都不用改。掛載路徑不寫死：按檔名遞迴找（可能是
#    /kaggle/input/<slug>/，也可能是 /kaggle/input/datasets/<owner>/<slug>/）。
#    找不到檔、大小不對就直接停掉，不要默默掉回下載——那會燒掉
#    一次下載的時間，還讓人以為用了緩存。
log("== 3. models: dataset mount or download ==")
USE_DATASET = bool(OPTS.get("use_dataset"))
DATASET_FILES = [
    ("qwen-image-2.1-Q4_K_M.gguf", 4199565024, "diffusion_models"),
    ("qwen3vl_8b_int8_convrot.safetensors", 9350798360, "text_encoders"),
    ("qwen_image_2.1_vae_bf16.safetensors", 675509688, "vae"),
]
dataset_info = {"use_dataset": USE_DATASET, "files_downloaded_bytes": 0}
if USE_DATASET:
    t_locate = time.time()
    found = {}
    for name, expected, category in DATASET_FILES:
        path = find_dataset_file(name, expected)
        found[category] = path
        log(f"dataset file OK: {name} -> {path} "
            f"({os.path.getsize(path)} bytes)")
    dataset_info["dataset_locate_seconds"] = round(time.time() - t_locate, 1)
    # 首次讀探針：各讀 1MB，量掛載目錄第一次讀要多久（FUSE 冷緩存
    # 的代價）；模型完整載入發生在生圖時，算在 generation_seconds 裡。
    t_read = time.time()
    for name, expected, category in DATASET_FILES:
        with open(found[category], "rb") as f:
            f.read(1024 * 1024)
    dataset_info["dataset_first_read_seconds"] = round(time.time() - t_read, 1)
    yaml_path = write_extra_model_paths(COMFY, found)
    dataset_info["dataset_bytes"] = 14225873072
    log(f"dataset ready: locate {dataset_info['dataset_locate_seconds']}s + "
        f"first-read {dataset_info['dataset_first_read_seconds']}s; "
        f"extra_model_paths.yaml -> {yaml_path}")
else:
    FILES = [
        ("https://huggingface.co/unsloth/Qwen-Image-2.1-GGUF/resolve/main/qwen-image-2.1-Q4_K_M.gguf",
         f"{COMFY}/models/diffusion_models/qwen-image-2.1-Q4_K_M.gguf",
         4199565024),
        ("https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
         f"{COMFY}/models/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
         9350798360),
        ("https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors",
         f"{COMFY}/models/vae/qwen_image_2.1_vae_bf16.safetensors",
         675509688),
    ]
    for url, dest, expected in FILES:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest) and os.path.getsize(dest) == expected:
            log(f"already present, size OK: {dest}")
            continue
        # v4 fix: aria2c -o is a bare filename, NOT a full path. Passing the
        # full path as -o made aria2 write to /kaggle/working/<full path>
        # (v3: file landed at /kaggle/working/kaggle/tmp/... and the
        # getsize check on the absolute path then raised FileNotFoundError).
        # Use -d <dir> -o <basename> instead.
        run(["aria2c", "-x", "8", "-s", "8", "-c",
             "-d", os.path.dirname(dest), "-o", os.path.basename(dest), url])
        got = os.path.getsize(dest)
        log(f"downloaded {dest} size={got} expected={expected}")
        if got != expected:
            raise SystemExit(f"SIZE MISMATCH {dest}: got {got}, expected {expected}")
    dataset_info["files_downloaded_bytes"] = 14225873072

# 4. Start ComfyUI headless with the T4 flags verified on real T4s.
# --force-fp16 and --disable-comfy-compiler are NOT optional on Turing:
# without them the fp16 path crashes or runs at 38 s/step.
log("== 4. start ComfyUI ==")
logf = open(f"{WORK}/comfyui.log", "w")
proc = subprocess.Popen(
    [sys.executable, "main.py", "--force-fp16", "--disable-comfy-compiler",
     "--lowvram", "--listen", "127.0.0.1", "--port", "8188"],
    cwd=COMFY, stdout=logf, stderr=subprocess.STDOUT)
try:
    for _ in range(60):
        time.sleep(10)
        try:
            stats = http_get_json("http://127.0.0.1:8188/system_stats", timeout=5)
            log(f"ComfyUI up: {stats}")
            break
        except Exception as e:
            log(f"waiting for ComfyUI: {e}")
    else:
        log("ComfyUI did not come up in 10 minutes; last log lines:")
        try:
            logf.flush()
            with open(f"{WORK}/comfyui.log") as lf:
                log("".join(lf.readlines()[-40:]))
        except Exception as e:
            log(f"could not read comfyui.log: {e}")
        raise SystemExit("ComfyUI did not come up in 10 minutes")

    # 5. Submit 1 workflow via the API. 尺寸、步數、CFG、種子都從選項來，
    # 預設就是已驗證的 v4 配方（768x768、20 步、cfg 1、euler）。
    # Workflow is embedded here so the script does not depend on a
    # separate file being present in /kaggle/working at run time.
    log("== 5. generate 1 image ==")
    if OPTS.get("mode") in ("single", "multi"):
        # 參考生圖：先把參考圖從工作台的公開路由下載到
        # ComfyUI 的 input 目錄。第一張圖按選的尺寸縮好，
        # 它決定出圖畫布；後面的圖保持原尺寸。
        input_dir = os.path.join(COMFY, "input")
        os.makedirs(input_dir, exist_ok=True)
        ref_urls = OPTS.get("ref_urls") or []
        if not ref_urls:
            raise SystemExit("參考模式沒有參考圖網址")
        from PIL import Image
        for i, url in enumerate(ref_urls, 1):
            local = os.path.join(input_dir, f"ref_{i}.jpg")
            log(f"downloading ref {i}: {url}")
            req = urllib.request.Request(
                url, headers={"User-Agent": "qwen-webui/2.4"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            with open(local, "wb") as f:
                f.write(data)
            im = Image.open(local)
            im.load()
            im = im.convert("RGB")
            if i == 1:
                im = im.resize((int(OPTS["width"]), int(OPTS["height"])),
                               Image.LANCZOS)
            im.save(local, "JPEG", quality=92)
            log(f"ref {i} saved: {local} size={im.size}")
        log(f"denoise={1.0 - float(OPTS.get('ref_strength', 0.7)):.2f} "
            f"(參考強度 {float(OPTS.get('ref_strength', 0.7)):.2f})")
    workflow = build_workflow(OPTS, PROMPT)
    body = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8188/prompt", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        prompt_id = json.load(resp)["prompt_id"]
    log(f"prompt_id={prompt_id}")
    gen_start = time.time()
    out_png = None
    for _ in range(120):
        time.sleep(10)
        hist = http_get_json(f"http://127.0.0.1:8188/history/{prompt_id}", timeout=10)
        entry = hist.get(prompt_id)
        if entry and entry.get("status", {}).get("completed"):
            outs = entry["outputs"]
            for node_out in outs.values():
                for img in node_out.get("images", []):
                    out_png = img
            break
        log("generating...")
    if not out_png:
        raise SystemExit("generation did not complete in 20 minutes")
    log(f"generation took {time.time() - gen_start:.1f}s, output={out_png}")

    # 6. Copy the PNG to /kaggle/working and verify it.
    src = os.path.join(COMFY, "output", out_png["subfolder"], out_png["filename"])
    dst = os.path.join(WORK, "qwen_test_chufeng.png")
    run(["cp", src, dst])
    from PIL import Image
    im = Image.open(dst)
    log(f"saved {dst} size={os.path.getsize(dst)} bytes dimensions={im.size} mode={im.mode}")
    summary = {
        "prompt_id": prompt_id,
        "generation_seconds": round(time.time() - gen_start, 1),
        "total_seconds": round(time.time() - START, 1),
        "output_file": "qwen_test_chufeng.png",
        "output_bytes": os.path.getsize(dst),
        "dimensions": list(im.size),
    }
    # 數據集模式的耗時也寫進去，第一次真跑時就能量出到底省了多少：
    # dataset_locate_seconds（找檔）、dataset_first_read_seconds
    # （首次讀 1MB 探針）、files_downloaded_bytes（數據集模式是 0）。
    summary.update(dataset_info)
    with open(os.path.join(WORK, "test_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    log("== DONE ==")
finally:
    proc.terminate()
