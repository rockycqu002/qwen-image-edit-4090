#!/usr/bin/env python3
"""Protocol / format / limit tests against a live endpoint (requirement §6.1 and §6.6).

  python tests/run_protocol.py --endpoint <id> [--out results/protocol.csv]

Generates its own synthetic test images, drives /run → /status → base64 decode for every case, asserts the
contract (JPEG signature, size cap, delayTime/executionTime present, no nested output) and records job id, worker
id, terminal status, timings, output bytes and the info block. Raw returned bytes are saved untouched.
"""
import argparse, base64, csv, io, json, os, random, sys, time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
import rp  # noqa: E402

PROMPT = "Change the background to a plain light blue wall. Keep the subject unchanged."
MAX_OUT_B64 = int(9.5 * 1024 * 1024)


def scene(w, h, seed=0):
    im = Image.new("RGB", (w, h), (235, 228, 210))
    d = ImageDraw.Draw(im)
    d.rectangle((w * 0.3, h * 0.35, w * 0.7, h * 0.9), fill=(40, 90, 160))
    d.ellipse((w * 0.38, h * 0.12, w * 0.62, h * 0.4), fill=(230, 190, 150))
    d.text((10, 10), f"synthetic {w}x{h} seed{seed}", fill=(20, 20, 20))
    return im


def rgb_noise(w, h, seed=1):
    return Image.merge("RGB", [Image.effect_noise((w, h), 90 + 10 * i).point(lambda v, o=i * 40: (v + o) % 256) for i in range(3)])


def enc(im, fmt, **kw):
    buf = io.BytesIO(); im.save(buf, format=fmt, **kw); return buf.getvalue()


def b64(b):
    return base64.b64encode(b).decode()


def cases():
    yield "jpeg_landscape_1280", {"image": b64(enc(scene(1280, 853), "JPEG", quality=92)), "prompt": PROMPT}, ["COMPLETED"]
    yield "png_portrait_960", {"image": b64(enc(scene(960, 1280), "PNG")), "prompt": PROMPT}, ["COMPLETED"]
    yield "webp_square_1024", {"image": b64(enc(scene(1024, 1024), "WEBP", quality=90)), "prompt": PROMPT}, ["COMPLETED"]
    yield "png_rgba_transparent", {"image": b64(enc(scene(800, 600).convert("RGBA"), "PNG")), "prompt": PROMPT}, ["COMPLETED"]
    yield "data_url_prefix", {"image": "data:image/jpeg;base64," + b64(enc(scene(640, 480), "JPEG")), "prompt": PROMPT}, ["COMPLETED"]
    yield "small_320", {"image": b64(enc(scene(320, 240), "JPEG")), "prompt": PROMPT}, ["COMPLETED"]
    exif = Image.Exif(); exif[0x0112] = 6
    yield "jpeg_exif_rotated", {"image": b64(enc(scene(1200, 800), "JPEG", exif=exif.tobytes())), "prompt": PROMPT}, ["COMPLETED"]
    frames = [scene(512, 512, s) for s in range(3)]
    buf = io.BytesIO(); frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:], duration=80)
    yield "animated_webp_first_frame", {"image": b64(buf.getvalue()), "prompt": PROMPT}, ["COMPLETED"]
    yield "two_refs", {"image": b64(enc(scene(1024, 768), "JPEG")), "ref_image": b64(enc(scene(768, 768, 1), "JPEG")),
                       "prompt": "Put the round object from the second image on top of the blue box in the first image."}, ["COMPLETED"]
    yield "params_steps12_res896_seed", {"image": b64(enc(scene(1024, 768), "JPEG")), "prompt": PROMPT, "steps": 12, "resolution": 896, "seed": 12345}, ["COMPLETED"]
    # input near the caller's 8 MiB / platform 10 MB limits: 3-channel noise, target ≈ 7.0 MiB raw
    noise = rgb_noise(1500, 1500)
    for q in (100, 98, 96, 94, 92, 90):
        raw = enc(noise, "JPEG", quality=q, subsampling=0)
        if len(raw) <= 7.0 * 1024 * 1024:
            break
    yield f"big_jpeg_{len(raw) // 1024}KiB_payload{len(b64(raw)) // 1000}kB", {"image": b64(raw), "prompt": PROMPT}, ["COMPLETED", "HTTP 413"]
    # heaviest allowed parameters: 30 steps at 1280 (also the worst case for the 60 s timeout)
    yield "max_params_steps30_res1280", {"image": b64(enc(scene(1280, 1280), "JPEG")), "prompt": PROMPT, "steps": 30, "resolution": 1280}, ["COMPLETED"]
    rnd = random.Random(1234)
    yield "invalid_base64", {"image": "@@@not-base64@@@", "prompt": PROMPT}, ["FAILED"]
    yield "corrupt_jpeg", {"image": b64(b"\xff\xd8\xff\xe0" + bytes(rnd.getrandbits(8) for _ in range(3000))), "prompt": PROMPT}, ["FAILED"]
    yield "gif_unsupported", {"image": b64(enc(scene(200, 200), "GIF")), "prompt": PROMPT}, ["FAILED"]
    yield "missing_prompt", {"image": b64(enc(scene(400, 300), "JPEG"))}, ["FAILED"]
    yield "missing_image", {"prompt": PROMPT}, ["FAILED"]
    yield "steps_out_of_range", {"image": b64(enc(scene(400, 300), "JPEG")), "prompt": PROMPT, "steps": 99}, ["FAILED"]
    yield "seven_images_too_many", {"image": b64(enc(scene(256, 256), "JPEG")), "ref_images": [b64(enc(scene(256, 256, i), "JPEG")) for i in range(6)], "prompt": PROMPT}, ["FAILED"]
    # 8 MiB + 10 B raw is ~11.2 MB after base64 → the PLATFORM should reject it before any handler runs
    yield "over_8mib_png_platform_limit", {"image": b64(b"\x89PNG\r\n\x1a\n" + b"\0" * (8 * 1024 * 1024 + 10)), "prompt": PROMPT}, ["HTTP 413", "HTTP 400", "FAILED"]


def check_completed(doc, row, save_path):
    im, fmt, n = rp.decode_output(doc)
    out = doc["output"]; info = out.get("info") or {}
    b64s = out["image"]
    with open(save_path, "wb") as f:
        f.write(base64.b64decode(b64s.split(",", 1)[1] if b64s.startswith("data:") else b64s))
    problems = []
    if fmt != "JPEG": problems.append(f"format {fmt}")
    if len(b64s) > MAX_OUT_B64: problems.append("output over 9.5 MiB")
    if not isinstance(doc.get("delayTime"), int) or not isinstance(doc.get("executionTime"), int): problems.append("platform timings missing")
    if not info.get("gpu") or "4090" not in str(info.get("gpu")): problems.append(f"gpu={info.get('gpu')}")
    row.update(out_format=fmt, out_bytes=n, out_size=f"{im.width}x{im.height}", contract_problems=";".join(problems))
    return not problems


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--endpoint", required=True); ap.add_argument("--out", default="results/protocol.csv")
    ap.add_argument("--only", nargs="*"); ap.add_argument("--save-dir", default="results/protocol_images")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True); os.makedirs(a.save_dir, exist_ok=True)
    rows = []

    def record(name, expect, inp=None, policy=None):
        payload_mb = round(len(json.dumps({"input": inp})) / 1e6, 2) if inp is not None else None
        sub = rp.run(a.endpoint, inp, policy=policy)
        row = {"case": name, "expect": "|".join(expect), "payload_mb": payload_mb, "submit_ms": sub.get("submit_ms"), "submitted_at": sub.get("submitted_at")}
        if "submit_error" in sub:
            row.update(status=f"HTTP {sub['submit_error']}", error=sub["body"][:200]); row["ok"] = row["status"] in expect
            rows.append(row); print(row, flush=True); return None
        doc = rp.wait(a.endpoint, sub["id"])
        row.update(rp.summarize(doc))
        if doc.get("status") == "COMPLETED":
            try:
                good = check_completed(doc, row, os.path.join(a.save_dir, f"{name}.jpg"))
                row["ok"] = "COMPLETED" in expect and good
            except Exception as e:
                row.update(status="COMPLETED_BAD_OUTPUT", error=repr(e)[:200]); row["ok"] = False
        else:
            row["ok"] = doc.get("status") in expect and (doc.get("status") != "FAILED" or bool(doc.get("error")))
        rows.append(row); print({k: row[k] for k in ("case", "status", "delay_ms", "exec_ms", "wall_ms", "out_bytes", "out_size", "worker_id", "ok", "error") if k in row}, flush=True)
        return doc

    for name, inp, expect in cases():
        if a.only and name not in a.only:
            continue
        record(name, expect, inp)

    heavy = {"image": b64(enc(scene(1280, 1280), "JPEG")), "prompt": PROMPT, "steps": 30, "resolution": 1280}
    # TIMED_OUT via per-request policy (platform minimum 5 s; the heavy job needs ~18 s). Note: a timeout kills the worker.
    record("policy_timeout_5s", ["TIMED_OUT", "FAILED"], heavy, policy={"executionTimeout": 5000})
    # CANCELLED while queued, then while running, then prove the endpoint still serves jobs
    sub = rp.run(a.endpoint, heavy)
    if "id" in sub:
        c = rp.cancel(a.endpoint, sub["id"]); doc = rp.wait(a.endpoint, sub["id"])
        rows.append({"case": "cancel_while_queued", "expect": "CANCELLED", **rp.summarize(doc), "ok": doc.get("status") == "CANCELLED", "cancel_response": json.dumps(c)[:120]}); print(rows[-1], flush=True)
    sub = rp.run(a.endpoint, heavy)
    if "id" in sub:
        t0 = time.time()
        while time.time() - t0 < 60 and rp.status(a.endpoint, sub["id"]).get("status") != "IN_PROGRESS":
            time.sleep(0.5)
        c = rp.cancel(a.endpoint, sub["id"]); doc = rp.wait(a.endpoint, sub["id"])
        rows.append({"case": "cancel_while_running", "expect": "CANCELLED", **rp.summarize(doc), "ok": doc.get("status") == "CANCELLED", "cancel_response": json.dumps(c)[:120]}); print(rows[-1], flush=True)
    record("after_cancel_still_serving", ["COMPLETED"], {"image": b64(enc(scene(1024, 768), "JPEG")), "prompt": PROMPT})

    keys = sorted({k for r in rows for k in r})
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    print(f"\n{sum(1 for r in rows if r.get('ok'))}/{len(rows)} cases as expected → {a.out}")


if __name__ == "__main__":
    main()
