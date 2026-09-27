#!/usr/bin/env python3
"""Protocol / format / limit tests against a live endpoint (requirement §6.1 and §6.6).

  python tests/run_protocol.py --endpoint <id> [--out results/protocol.csv]

Generates its own synthetic test images (no third-party pictures), drives /run → /status → base64 decode for every
case and records job id, terminal status, delayTime, executionTime, wall time, output bytes and the info block.
"""
import argparse, base64, csv, io, json, os, sys, time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
import rp  # noqa: E402

PROMPT = "Change the background to a plain light blue wall. Keep the subject unchanged."


def scene(w, h, seed=0):
    im = Image.new("RGB", (w, h), (235, 228, 210))
    d = ImageDraw.Draw(im)
    d.rectangle((w * 0.3, h * 0.35, w * 0.7, h * 0.9), fill=(40, 90, 160))            # "subject"
    d.ellipse((w * 0.38, h * 0.12, w * 0.62, h * 0.4), fill=(230, 190, 150))
    d.text((10, 10), f"synthetic {w}x{h} seed{seed}", fill=(20, 20, 20))
    return im


def enc(im, fmt, **kw):
    buf = io.BytesIO(); im.save(buf, format=fmt, **kw); return buf.getvalue()


def b64(b):
    return base64.b64encode(b).decode()


def cases():
    yield "jpeg_landscape_1280", {"image": b64(enc(scene(1280, 853), "JPEG", quality=92)), "prompt": PROMPT}, "COMPLETED"
    yield "png_portrait_960", {"image": b64(enc(scene(960, 1280), "PNG")), "prompt": PROMPT}, "COMPLETED"
    yield "webp_square_1024", {"image": b64(enc(scene(1024, 1024), "WEBP", quality=90)), "prompt": PROMPT}, "COMPLETED"
    yield "png_rgba_transparent", {"image": b64(enc(scene(800, 600).convert("RGBA"), "PNG")), "prompt": PROMPT}, "COMPLETED"
    yield "data_url_prefix", {"image": "data:image/jpeg;base64," + b64(enc(scene(640, 480), "JPEG")), "prompt": PROMPT}, "COMPLETED"
    yield "small_320", {"image": b64(enc(scene(320, 240), "JPEG")), "prompt": PROMPT}, "COMPLETED"
    exif = Image.Exif(); exif[0x0112] = 6
    yield "jpeg_exif_rotated", {"image": b64(enc(scene(1200, 800), "JPEG", exif=exif.tobytes())), "prompt": PROMPT}, "COMPLETED"
    frames = [scene(512, 512, s) for s in range(3)]
    buf = io.BytesIO(); frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:], duration=80)
    yield "animated_webp_first_frame", {"image": b64(buf.getvalue()), "prompt": PROMPT}, "COMPLETED"
    yield "two_refs", {"image": b64(enc(scene(1024, 768), "JPEG")), "ref_image": b64(enc(scene(768, 768, 1), "JPEG")),
                       "prompt": "Put the round object from the second image on top of the blue box in the first image."}, "COMPLETED"
    yield "params_steps12_res896_seed", {"image": b64(enc(scene(1024, 768), "JPEG")), "prompt": PROMPT, "steps": 12, "resolution": 896, "seed": 12345}, "COMPLETED"
    # near the 8 MiB raw / 10 MB request limits: high-entropy JPEG (noise) at 1280 px
    noise = Image.effect_noise((1280, 1280), 80).convert("RGB")
    for q, tag in ((100, "big_jpeg_q100"),):
        raw = enc(noise, "JPEG", quality=q, subsampling=0)
        yield f"{tag}_{len(raw) // 1024}KiB", {"image": b64(raw), "prompt": PROMPT}, "COMPLETED_OR_413"
    yield "invalid_base64", {"image": "@@@not-base64@@@", "prompt": PROMPT}, "FAILED"
    yield "corrupt_jpeg", {"image": b64(b"\xff\xd8\xff\xe0" + os.urandom(3000)), "prompt": PROMPT}, "FAILED"
    yield "gif_unsupported", {"image": b64(enc(scene(200, 200), "GIF")), "prompt": PROMPT}, "FAILED"
    yield "missing_prompt", {"image": b64(enc(scene(400, 300), "JPEG"))}, "FAILED"
    yield "missing_image", {"prompt": PROMPT}, "FAILED"
    yield "steps_out_of_range", {"image": b64(enc(scene(400, 300), "JPEG")), "prompt": PROMPT, "steps": 99}, "FAILED"
    yield "over_8mib_png", {"image": b64(b"\x89PNG\r\n\x1a\n" + b"\0" * (8 * 1024 * 1024 + 10)), "prompt": PROMPT}, "FAILED_OR_413"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--endpoint", required=True); ap.add_argument("--out", default="results/protocol.csv")
    ap.add_argument("--only", nargs="*"); ap.add_argument("--save-dir", default="results/protocol_images")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True); os.makedirs(a.save_dir, exist_ok=True)
    rows = []
    for name, inp, expect in cases():
        if a.only and name not in a.only:
            continue
        payload_mb = len(json.dumps({"input": inp})) / 1e6
        sub = rp.run(a.endpoint, inp)
        row = {"case": name, "expect": expect, "payload_mb": round(payload_mb, 2), "submit_ms": sub.get("submit_ms")}
        if "submit_error" in sub:
            row.update(status=f"HTTP {sub['submit_error']}", error=sub["body"][:200]); rows.append(row); print(row, flush=True); continue
        doc = rp.wait(a.endpoint, sub["id"])
        row.update(job_id=sub["id"], status=doc.get("status"), delay_ms=doc.get("delayTime"), exec_ms=doc.get("executionTime"), wall_ms=doc.get("wall_ms"),
                   error=str(doc.get("error", ""))[:200])
        if doc.get("status") == "COMPLETED":
            try:
                im, fmt, n = rp.decode_output(doc)
                info = (doc.get("output") or {}).get("info") or {}
                row.update(out_format=fmt, out_bytes=n, out_size=f"{im.width}x{im.height}", seed=info.get("seed"), infer_ms=info.get("infer_ms"))
                im.save(os.path.join(a.save_dir, f"{name}.jpg"), quality=90)
            except Exception as e:
                row.update(status="COMPLETED_BAD_OUTPUT", error=repr(e)[:200])
        row["ok"] = (row["status"] == expect) or (expect == "COMPLETED_OR_413" and row["status"] in ("COMPLETED", "HTTP 413")) \
            or (expect == "FAILED_OR_413" and row["status"] in ("FAILED", "HTTP 413"))
        rows.append(row); print(row, flush=True)
    # TIMED_OUT via per-request policy, CANCELLED via /cancel
    inp = {"image": b64(enc(scene(1024, 768), "JPEG")), "prompt": PROMPT}
    sub = rp.run(a.endpoint, inp, policy={"executionTimeout": 2000})
    doc = rp.wait(a.endpoint, sub["id"]) if "id" in sub else {}
    rows.append({"case": "policy_timeout_2s", "expect": "TIMED_OUT", "job_id": sub.get("id"), "status": doc.get("status"), "wall_ms": doc.get("wall_ms"), "ok": doc.get("status") in ("TIMED_OUT", "FAILED")}); print(rows[-1], flush=True)
    sub = rp.run(a.endpoint, inp)
    if "id" in sub:
        time.sleep(0.5); rp.cancel(a.endpoint, sub["id"]); doc = rp.wait(a.endpoint, sub["id"])
        rows.append({"case": "cancel_after_submit", "expect": "CANCELLED", "job_id": sub["id"], "status": doc.get("status"), "wall_ms": doc.get("wall_ms"), "ok": doc.get("status") == "CANCELLED"}); print(rows[-1], flush=True)
    keys = sorted({k for r in rows for k in r})
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    print(f"\n{sum(1 for r in rows if r.get('ok'))}/{len(rows)} cases as expected → {a.out}")


if __name__ == "__main__":
    main()
