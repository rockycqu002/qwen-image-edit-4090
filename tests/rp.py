"""Minimal RunPod Serverless client used by every test script.

Reads the API key from $RUNPOD_API_KEY or ~/.config/runpod/api_key. Never prints the key or image payloads.
Transient transport errors (429/5xx/socket) on status polls are retried with backoff, like the browser caller does.
"""
import base64, datetime, io, json, os, socket, time, urllib.error, urllib.request

API = "https://api.runpod.ai/v2"
TERMINAL = ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED")


def api_key():
    k = os.environ.get("RUNPOD_API_KEY")
    if not k:
        with open(os.path.expanduser("~/.config/runpod/api_key")) as f:
            k = f.read().strip()
    return k


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _req(method, url, body=None, timeout=30):
    headers = {"Authorization": f"Bearer {api_key()}", "User-Agent": "qwen-image-edit-tests/1.0"}   # Cloudflare (error 1010) rejects Python-urllib's default UA
    data = None
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def run(endpoint, inp, policy=None, timeout=30):
    """POST /run. Returns the platform document plus submit_ms / submitted_at, or submit_error on HTTP/transport failure."""
    body = {"input": inp}
    if policy:
        body["policy"] = policy
    t0, at = time.time(), now_iso()
    try:
        _, j = _req("POST", f"{API}/{endpoint}/run", body, timeout)
    except urllib.error.HTTPError as e:
        return {"submit_error": e.code, "body": e.read()[:500].decode(errors="replace"), "submit_ms": int((time.time() - t0) * 1000), "submitted_at": at}
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        return {"submit_error": "transport", "body": repr(e)[:300], "submit_ms": int((time.time() - t0) * 1000), "submitted_at": at}
    j["submit_ms"] = int((time.time() - t0) * 1000); j["submitted_at"] = at
    return j


def status(endpoint, job_id, timeout=15, retries=5):
    delay = 1.0
    for attempt in range(retries):
        try:
            _, j = _req("GET", f"{API}/{endpoint}/status/{job_id}", None, timeout)
            return j
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay); delay = min(delay * 2, 8); continue
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError):
            if attempt < retries - 1:
                time.sleep(delay); delay = min(delay * 2, 8); continue
            raise


def cancel(endpoint, job_id):
    _, j = _req("POST", f"{API}/{endpoint}/cancel/{job_id}", {}, 15)
    return j


def health(endpoint):
    _, j = _req("GET", f"{API}/{endpoint}/health", None, 15)
    return j


def wait(endpoint, job_id, max_s=240, interval=1.0):
    """Poll like the browser (1 s) until a terminal status; returns the status document + wall_ms/finished_at/polls."""
    t0, polls = time.time(), 0
    while True:
        j = status(endpoint, job_id); polls += 1
        if j.get("status") in TERMINAL:
            j.update(wall_ms=int((time.time() - t0) * 1000), finished_at=now_iso(), polls=polls)
            return j
        if time.time() - t0 > max_s:
            j.update(wall_ms=int((time.time() - t0) * 1000), poll_timeout=True, polls=polls)
            return j
        time.sleep(interval)


def img_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def decode_output(doc):
    """Validate a COMPLETED status document; returns (PIL.Image, format, bytes). Raises on anything off-contract."""
    from PIL import Image
    out = doc.get("output")
    if not isinstance(out, dict):
        raise ValueError("output is not an object")
    if "output" in out or "error" in out:
        raise ValueError("output is nested or carries an error field")
    b64 = out.get("image")
    if not isinstance(b64, str) or not b64:
        raise ValueError("output.image missing")
    payload = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    blob = base64.b64decode(payload)
    im = Image.open(io.BytesIO(blob)); im.load()
    if im.format not in ("JPEG", "PNG", "WEBP"):
        raise ValueError(f"unsupported output format {im.format}")
    return im, im.format, len(blob)


def summarize(doc):
    """Flat, log-safe subset of a status document for CSV rows."""
    info = (doc.get("output") or {}).get("info") if isinstance(doc.get("output"), dict) else None
    info = info or {}
    return {"job_id": doc.get("id"), "status": doc.get("status"), "delay_ms": doc.get("delayTime"), "exec_ms": doc.get("executionTime"),
            "worker_id": doc.get("workerId"), "wall_ms": doc.get("wall_ms"), "polls": doc.get("polls"), "finished_at": doc.get("finished_at"),
            "seed": info.get("seed"), "infer_ms": info.get("infer_ms"), "gpu": info.get("gpu"), "vram_used_mib": info.get("vram_used_mib"),
            "rss_mib": info.get("rss_mib"), "worker_jobs": info.get("worker_jobs"), "init_s": info.get("init_s"),
            "error": str(doc.get("error", ""))[:200]}
