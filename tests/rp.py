"""Minimal RunPod Serverless client used by every test script.

Reads the API key from $RUNPOD_API_KEY or ~/.config/runpod/api_key. Never prints the key or image payloads.
"""
import base64, io, json, os, time, urllib.error, urllib.request

API = "https://api.runpod.ai/v2"


def api_key():
    k = os.environ.get("RUNPOD_API_KEY")
    if not k:
        with open(os.path.expanduser("~/.config/runpod/api_key")) as f:
            k = f.read().strip()
    return k


def _req(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def run(endpoint, inp, policy=None, timeout=30):
    body = {"input": inp}
    if policy:
        body["policy"] = policy
    t0 = time.time()
    try:
        status, j = _req("POST", f"{API}/{endpoint}/run", body, timeout)
    except urllib.error.HTTPError as e:
        return {"submit_error": e.code, "body": e.read()[:500].decode(errors="replace"), "submit_ms": int((time.time() - t0) * 1000)}
    j["submit_ms"] = int((time.time() - t0) * 1000)
    return j


def status(endpoint, job_id, timeout=15):
    _, j = _req("GET", f"{API}/{endpoint}/status/{job_id}", None, timeout)
    return j


def cancel(endpoint, job_id):
    _, j = _req("POST", f"{API}/{endpoint}/cancel/{job_id}", {}, 15)
    return j


def wait(endpoint, job_id, max_s=240, interval=1.0):
    """Poll like the browser does (1 s), return the terminal status document plus wall time."""
    t0 = time.time()
    while True:
        j = status(endpoint, job_id)
        if j.get("status") in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"):
            j["wall_ms"] = int((time.time() - t0) * 1000)
            return j
        if time.time() - t0 > max_s:
            j["wall_ms"] = int((time.time() - t0) * 1000); j["poll_timeout"] = True
            return j
        time.sleep(interval)


def health(endpoint):
    _, j = _req("GET", f"{API}/{endpoint}/health", None, 15)
    return j


def img_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def decode_output(doc):
    """Validate a COMPLETED status document: returns (PIL.Image, format, bytes) or raises."""
    from PIL import Image
    out = doc.get("output") or {}
    b64 = out.get("image")
    if not isinstance(b64, str) or not b64:
        raise ValueError("output.image missing")
    payload = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    blob = base64.b64decode(payload)
    im = Image.open(io.BytesIO(blob)); im.load()
    return im, im.format, len(blob)
