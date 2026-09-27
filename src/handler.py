"""RunPod Serverless handler: Qwen-Image-2.1 (int8 convrot) single-image editing on RTX 4090 via ComfyUI.

Protocol (compatible with the previous FireRed-Edit endpoint):
  input.image      raw base64 (data: prefix tolerated)  -> Picture 1 (required)
  input.prompt     edit instruction, passed through unchanged (required)
  input.ref_image  optional base64                      -> Picture 2
  input.ref_images optional list of base64              -> Picture 3..6
  input.steps / input.resolution / input.seed           optional, clamped to safe ranges
returns {"image": <JPEG q95 base64>, "info": {...}} or {"error": "..."} (which RunPod reports as FAILED).

ComfyUI runs as a child process; its three model files are loaded once at worker start (warm-up) and reused.
"""
import atexit, base64, binascii, glob, io, json, os, secrets, signal, subprocess, sys, threading, time, traceback, uuid, urllib.error, urllib.parse, urllib.request

os.environ.setdefault("RUNPOD_LOG_LEVEL", "WARN")   # the SDK would otherwise log job payloads (base64) at DEBUG/INFO

from PIL import Image, ImageOps

COMFY_DIR = os.environ.get("COMFY_DIR", "/app/ComfyUI")
COMFY_URL = "http://127.0.0.1:8188"
IN_DIR, OUT_DIR, TMP_DIR = "/tmp/comfy_in", "/tmp/comfy_out", "/tmp/comfy_tmp"
MODELS = {"unet": "qwen_image_2.1_int8_convrot.safetensors", "clip": "qwen3vl_8b_int8_convrot.safetensors", "vae": "qwen_image_2.1_vae_bf16.safetensors"}
DEFAULTS = {"steps": int(os.environ.get("QIE_STEPS", 20)), "resolution": int(os.environ.get("QIE_RESOLUTION", 1024))}
LIMITS = {"steps": (4, 30), "resolution": (768, 1280), "seed": (0, 2**53)}
MAX_INPUT_BYTES = 8 * 1024 * 1024        # raw file size before base64 (caller contract)
MAX_PIXELS = 50_000_000                  # decompression-bomb guard; an 8 MiB JPEG never reaches this
MAX_IMAGES = 6                           # Picture 1..6
MAX_RETURN_BYTES = int(9.5 * 1024 * 1024)  # RunPod /run result limit is 10 MB including JSON
JPEG_QUALITIES = (95, 90, 85)
JOB_DEADLINE_S = float(os.environ.get("QIE_JOB_DEADLINE_S", 55))  # stay under the 60 s endpoint executionTimeout
MAGIC = {b"\xff\xd8\xff": "jpeg", b"\x89PNG\r\n\x1a\n": "png", b"RIFF": "webp"}

_proc = None
_log_fh = None
_state = {"gpu": None, "init_s": None, "jobs": 0}


# ----------------------------------------------------------------------------- logging / ComfyUI lifecycle
def log(msg, **kv):
    extra = (" " + json.dumps(kv, ensure_ascii=False)) if kv else ""
    print(f"[qie {time.strftime('%H:%M:%S')}] {msg}{extra}", flush=True)


def comfy(path, data=None, headers=None, raw=False, timeout=60):
    req = urllib.request.Request(COMFY_URL + path, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"error": body[:600].decode(errors="replace")}
        raise RuntimeError(f"ComfyUI HTTP {e.code}: " + json.dumps(payload.get("node_errors") or payload.get("error") or payload, ensure_ascii=False)[:700])
    if raw:
        return body
    return json.loads(body) if body.strip() else {}


def _tail_log(fh):
    """Forward ComfyUI's log file to stdout so RunPod captures it; never let the child block on a pipe."""
    with open(fh.name, "r", errors="replace") as f:
        while True:
            line = f.readline()
            if line:
                sys.stdout.write("[comfy] " + line); sys.stdout.flush()
            else:
                time.sleep(0.2)


def start_comfy():
    global _proc, _log_fh
    for d in (IN_DIR, OUT_DIR, TMP_DIR):
        os.makedirs(d, exist_ok=True)
    first = _log_fh is None
    _log_fh = open("/tmp/comfy.log", "a")
    cmd = [sys.executable, "main.py", "--listen", "127.0.0.1", "--port", "8188", "--use-sage-attention", "--disable-auto-launch",
           "--dont-print-server", "--disable-metadata", "--input-directory", IN_DIR, "--output-directory", OUT_DIR, "--temp-directory", TMP_DIR,
           "--extra-model-paths-config", os.path.join(COMFY_DIR, "extra_model_paths.yaml")]
    _proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=_log_fh, stderr=subprocess.STDOUT, start_new_session=True)
    if first:
        threading.Thread(target=_tail_log, args=(_log_fh,), daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < 300:
        if _proc.poll() is not None:
            raise RuntimeError(f"ComfyUI exited during start (code {_proc.returncode})")
        try:
            comfy("/system_stats", timeout=5); break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("ComfyUI did not answer /system_stats within 300 s")
    log("comfyui up", seconds=round(time.time() - t0, 1))


def stop_comfy(*_):
    if _proc and _proc.poll() is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM); _proc.wait(timeout=10)
        except Exception:
            try: os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except Exception: pass


def comfy_alive():
    if _proc is None or _proc.poll() is not None:
        return False
    try:
        comfy("/system_stats", timeout=5); return True
    except Exception:
        return False


def gpu_fingerprint():
    info = {}
    try:
        info["nvidia_smi"] = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10).stdout.strip()
        q = subprocess.run(["nvidia-smi", "--query-gpu=driver_version,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip()
        info["driver_memory"] = q
    except Exception as e:
        info["nvidia_smi_error"] = repr(e)
    try:
        s = comfy("/system_stats")
        d = s["devices"][0]
        info.update(gpu=d["name"], vram_total_mib=d["vram_total"] // 2**20, torch=s["system"].get("pytorch_version"), comfyui=s["system"].get("comfyui_version"), argv=s["system"].get("argv"))
    except Exception as e:
        info["system_stats_error"] = repr(e)
    try:
        info["manifest"] = json.load(open("/models/MANIFEST.json"))
    except Exception:
        pass
    return info


def vram_used_mib():
    try:
        d = comfy("/system_stats")["devices"][0]
        return (d["vram_total"] - d["vram_free"]) // 2**20
    except Exception:
        return None


def rss_mib():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None


# ----------------------------------------------------------------------------- input handling
class BadInput(Exception):
    pass


def decode_image(field, b64, index):
    if not isinstance(b64, str) or not b64.strip():
        raise BadInput(f"{field} 缺失或不是字符串")
    payload = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    try:
        blob = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        try:
            blob = base64.b64decode(payload + "=" * (-len(payload) % 4))
        except Exception:
            raise BadInput(f"{field} 不是有效的 base64")
    if not blob:
        raise BadInput(f"{field} 为空")
    if len(blob) > MAX_INPUT_BYTES:
        raise BadInput(f"{field} 解码后 {len(blob)} 字节，超过 8 MiB 上限")
    fmt = next((v for m, v in MAGIC.items() if blob.startswith(m)), None)
    if fmt == "webp" and blob[8:12] != b"WEBP":
        fmt = None
    if not fmt:
        raise BadInput(f"{field} 不是 JPEG / PNG / WebP")
    try:
        im = Image.open(io.BytesIO(blob))            # header only; the pixel guard runs before decoding
        if im.width * im.height > MAX_PIXELS:
            raise BadInput(f"{field} 像素数 {im.width}x{im.height} 超过上限")
        im.load()                                     # first frame only for animated WebP/APNG
    except BadInput:
        raise
    except Exception as e:
        raise BadInput(f"{field} 无法解码: {type(e).__name__}")
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255)); bg.paste(im, mask=im.split()[-1]); im = bg
    else:
        im = im.convert("RGB")
    return im, {"format": fmt, "bytes": len(blob), "width": im.width, "height": im.height}


def clamp_int(v, name, default):
    if v is None or v == "":
        return default
    try:
        v = int(v) if not isinstance(v, float) else int(v)
        if isinstance(v, bool):
            raise ValueError
    except (TypeError, ValueError):
        raise BadInput(f"{name} 不是整数")
    lo, hi = LIMITS[name]
    if not (lo <= v <= hi):
        raise BadInput(f"{name}={v} 超出允许范围 {lo}–{hi}")
    return v


def parse_job(inp):
    if not isinstance(inp, dict):
        raise BadInput("input 必须是对象")
    prompt = inp.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadInput("prompt 缺失")
    refs = inp.get("ref_images") or []
    if not isinstance(refs, list):
        raise BadInput("ref_images 必须是数组")
    fields = [("image", inp.get("image"))] + ([("ref_image", inp["ref_image"])] if inp.get("ref_image") not in (None, "") else []) + \
             [(f"ref_images[{i}]", v) for i, v in enumerate(refs) if v not in (None, "")]
    if len(fields) > MAX_IMAGES:
        raise BadInput(f"参考图最多 {MAX_IMAGES} 张（含 image）")
    images, metas = [], []
    for field, val in fields:
        im, meta = decode_image(field, val, len(images) + 1)
        images.append(im); metas.append(meta)
    steps = clamp_int(inp.get("steps"), "steps", DEFAULTS["steps"])
    resolution = clamp_int(inp.get("resolution"), "resolution", DEFAULTS["resolution"])
    resolution -= resolution % 32
    seed = clamp_int(inp.get("seed"), "seed", None)
    if seed is None:
        seed = secrets.randbelow(2**32)
    return {"prompt": prompt, "images": images, "image_meta": metas, "steps": steps, "resolution": resolution, "seed": seed}


# ----------------------------------------------------------------------------- ComfyUI graph
def build_graph(p, image_files, prefix):
    g = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
         "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "qwen_image", "device": "default"}},
         "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae"]}}}
    enc = {"clip": ["2", 0], "prompt": p["prompt"], "negative_prompt": "", "vae": ["3", 0], "resolution": p["resolution"]}
    for i, name in enumerate(image_files, 1):
        g[f"4{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{i}"] = [f"4{i}", 0]
    g["5"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
    g["7"] = {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": ["5", 2],
                                                   "seed": p["seed"], "steps": p["steps"], "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    g["9"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}}
    return g


def run_graph(graph, deadline_s):
    r = comfy("/prompt", json.dumps({"prompt": graph, "client_id": "qie"}).encode(), {"Content-Type": "application/json"})
    pid = r.get("prompt_id")
    if not pid:
        raise RuntimeError("ComfyUI 拒绝工作流: " + json.dumps(r.get("node_errors") or r, ensure_ascii=False)[:800])
    t0 = time.time()
    while True:
        h = comfy(f"/history/{pid}")
        if pid in h:
            break
        if time.time() - t0 > deadline_s:
            try:
                comfy("/queue", json.dumps({"delete": [pid]}).encode(), {"Content-Type": "application/json"}, raw=True)
                comfy("/interrupt", b"", {"Content-Type": "application/json"}, raw=True)
            except Exception: pass
            raise RuntimeError(f"推理超过 {deadline_s:.0f} s，已中断")
        time.sleep(0.1)
    st = h[pid]["status"]
    if st.get("status_str") != "success":
        for m in st.get("messages", []):
            if m[0] == "execution_error":
                raise RuntimeError(f"ComfyUI 执行失败（{m[1].get('node_type')}）: {m[1].get('exception_message', '')[:600]}")
        raise RuntimeError("ComfyUI 执行失败: " + json.dumps(st, ensure_ascii=False)[:600])
    outs = [o for n in h[pid]["outputs"].values() for o in n.get("images", [])]
    if not outs:
        raise RuntimeError("ComfyUI 没有产生输出图")
    o = outs[0]
    return os.path.join(OUT_DIR, o.get("subfolder", ""), o["filename"]), time.time() - t0


def encode_jpeg(im):
    for q in JPEG_QUALITIES:
        buf = io.BytesIO(); im.save(buf, format="JPEG", quality=q, subsampling=0 if q >= 95 else 1)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= MAX_RETURN_BYTES:
            return b64, q, len(buf.getvalue())
    raise RuntimeError("输出图片编码后仍超过 9.5 MB 结果上限")


# ----------------------------------------------------------------------------- warm-up & handler
def _synthetic(w, h):
    im = Image.new("RGB", (w, h), (200, 210, 220))
    px = im.load()
    for y in range(0, h, 8):
        for x in range(0, w, 8):
            c = (x * 255 // w, y * 255 // h, 128)
            for dy in range(8):
                for dx in range(8):
                    px[min(x + dx, w - 1), min(y + dy, h - 1)] = c
    return im


def warmup():
    t0 = time.time()
    prompts = ("Keep the image exactly as it is.",
               "Keep the image exactly as it is, preserving every object, colour, texture and the overall composition of the scene.")
    for (w, h), prompt in zip(((1024, 1024), (1152, 864)), prompts):
        name = f"warmup_{w}x{h}.png"; _synthetic(w, h).save(os.path.join(IN_DIR, name))
        p = {"prompt": prompt, "steps": 4, "resolution": 1024, "seed": 1}
        path, _ = run_graph(build_graph(p, [name], f"warmup/{w}x{h}"), 240)
        os.remove(path); os.remove(os.path.join(IN_DIR, name))
    log("warmup done", seconds=round(time.time() - t0, 1), vram_used_mib=vram_used_mib(), rss_mib=rss_mib())


def _cleanup(paths):
    for p in paths:
        try: os.remove(p)
        except Exception: pass


def handler(job):
    jid = str(job.get("id") or uuid.uuid4().hex[:12])
    t_start = time.time()
    paths = []
    try:
        p = parse_job(job.get("input"))
        if not comfy_alive():
            log("comfyui not alive; failing job and asking RunPod to replace this worker", job=jid, error_type="ComfyDead")
            return {"error": "inference backend unavailable, worker is being replaced", "refresh_worker": True}
        for i, im in enumerate(p["images"], 1):
            fn = f"{jid}_{i}.png"; im.save(os.path.join(IN_DIR, fn), format="PNG", compress_level=1); paths.append(os.path.join(IN_DIR, fn))
        graph = build_graph(p, [os.path.basename(x) for x in paths], f"job/{jid}")
        out_path, infer_s = run_graph(graph, max(5.0, JOB_DEADLINE_S - (time.time() - t_start)))
        paths.append(out_path)
        if jid not in os.path.basename(out_path):
            raise RuntimeError("输出文件名与任务不匹配")
        out = Image.open(out_path).convert("RGB")
        b64, q, nbytes = encode_jpeg(out)
        _state["jobs"] += 1
        info = {"model": "qwen-image-2.1-int8-convrot", "seed": p["seed"], "steps": p["steps"], "resolution": p["resolution"],
                "width": out.width, "height": out.height, "n_images": len(p["images"]), "jpeg_quality": q, "output_bytes": nbytes,
                "exec_ms": int((time.time() - t_start) * 1000), "infer_ms": int(infer_s * 1000), "gpu": (_state["gpu"] or {}).get("gpu"),
                "vram_used_mib": vram_used_mib(), "rss_mib": rss_mib(), "worker_jobs": _state["jobs"], "init_s": _state["init_s"]}
        log("job ok", job=jid, **{k: v for k, v in info.items() if k != "model"}, inputs=p["image_meta"])
        return {"image": b64, "info": info}
    except BadInput as e:
        log("job rejected", job=jid, error=str(e), error_type="BadInput")
        return {"error": f"invalid input: {e}"}
    except Exception as e:
        log("job failed", job=jid, error=str(e)[:800], error_type=type(e).__name__, tb=traceback.format_exc()[-1500:])
        if not comfy_alive():
            return {"error": f"{type(e).__name__}: {str(e)[:600]}", "refresh_worker": True}
        return {"error": f"{type(e).__name__}: {str(e)[:600]}"}
    finally:
        _cleanup(paths + glob.glob(os.path.join(OUT_DIR, "job", f"{jid}*")))


def main():
    t0 = time.time()
    atexit.register(stop_comfy)
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: (stop_comfy(), sys.exit(0)))
    start_comfy()
    _state["gpu"] = gpu_fingerprint()
    log("fingerprint", **_state["gpu"])
    warmup()
    _state["init_s"] = round(time.time() - t0, 1)
    log("worker ready", init_seconds=_state["init_s"])
    import runpod
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":
    main()
