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

# 3. Download the 3 files to scratch. One quantisation only, no BF16.
log("== 3. download models to scratch ==")
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

    # 5. Submit 1 workflow via the API. 768x768, 20 steps, cfg 1, euler.
    # Workflow is embedded here so the script does not depend on a
    # separate file being present in /kaggle/working at run time.
    log("== 5. generate 1 test image ==")
    workflow = {
        "1": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "qwen-image-2.1-Q4_K_M.gguf"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                         "type": "qwen_image"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0],
                         "text": __USER_PROMPT_JSON__}},
        "9": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": "blurry, low quality, distorted"}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"batch_size": 1, "height": 768, "width": 768}},
        "6": {"class_type": "KSampler",
              "inputs": {"cfg": 1.0, "denoise": 1.0, "latent_image": ["5", 0],
                         "model": ["1", 0], "negative": ["9", 0],
                         "positive": ["4", 0], "sampler_name": "euler",
                         "scheduler": "simple", "seed": 42, "steps": 20}},
        "7": {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "qwen_test_chufeng",
                         "images": ["7", 0]}},
    }
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
    with open(os.path.join(WORK, "test_summary.json"), "w") as f:
        json.dump({
            "prompt_id": prompt_id,
            "generation_seconds": round(time.time() - gen_start, 1),
            "total_seconds": round(time.time() - START, 1),
            "output_file": "qwen_test_chufeng.png",
            "output_bytes": os.path.getsize(dst),
            "dimensions": list(im.size),
            "files_downloaded_bytes": 14225873072,
        }, f, indent=2)
    log("== DONE ==")
finally:
    proc.terminate()
