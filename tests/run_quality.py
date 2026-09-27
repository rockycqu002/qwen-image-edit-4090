#!/usr/bin/env python3
"""Quality pairs: the same inputs + caller prompts through the OLD endpoint (5090, FireRed-Edit) and the NEW one
(4090, Qwen-Image-2.1 int8). Requirement §6.5. Sends exactly one job per pair per endpoint (approved: ~10 jobs to
the production endpoint).

  python tests/run_quality.py --old w2ww53ksg9wtj7 --new <id> --pairs tests/quality_pairs.json [--out results/quality]

`quality_pairs.json`: [{"name": "...", "image": "path/to/file.jpg", "prompt": "..."}]. Both raw outputs are saved
untouched, per-pair metadata (status, timings, seed/params from info, output size/bytes) goes to quality.csv, and a
side-by-side contact sheet (input | old | new) is written for human review.
"""
import argparse, base64, csv, io, json, os, sys, time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
import rp  # noqa: E402


def one(endpoint, image_path, prompt):
    doc = rp.run(endpoint, {"image": rp.img_b64(image_path), "prompt": prompt})
    if "id" not in doc:
        return {"status": f"HTTP {doc.get('submit_error')}", "error": doc.get("body", "")[:200]}, None
    doc = rp.wait(endpoint, doc["id"], max_s=300)
    row = rp.summarize(doc); blob = None
    if doc.get("status") == "COMPLETED":
        try:
            im, fmt, n = rp.decode_output(doc); b = doc["output"]["image"]; blob = base64.b64decode(b.split(",", 1)[1] if b.startswith("data:") else b)
            row.update(out_format=fmt, out_bytes=n, out_size=f"{im.width}x{im.height}", info=json.dumps((doc.get("output") or {}).get("info"), ensure_ascii=False)[:300])
        except Exception as e:
            row.update(status="COMPLETED_BAD_OUTPUT", error=repr(e)[:200])
    return row, blob


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--old", required=True); ap.add_argument("--new", required=True)
    ap.add_argument("--pairs", required=True); ap.add_argument("--out", default="results/quality")
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    pairs = json.load(open(a.pairs)); rows = []
    for p in pairs:
        rec = {"name": p["name"], "prompt": p["prompt"], "image": p["image"]}
        for tag, ep in (("old", a.old), ("new", a.new)):
            t0 = time.time(); row, blob = one(ep, p["image"], p["prompt"])
            rec.update({f"{tag}_{k}": v for k, v in row.items()}); rec[f"{tag}_e2e_ms"] = int((time.time() - t0) * 1000)
            if blob:
                ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(row.get("out_format"), "bin")
                with open(os.path.join(a.out, f"{p['name']}_{tag}.{ext}"), "wb") as f:
                    f.write(blob)
        rows.append(rec); print({k: rec.get(k) for k in ("name", "old_status", "old_exec_ms", "old_out_size", "new_status", "new_exec_ms", "new_out_size")}, flush=True)
    keys = sorted({k for r in rows for k in r})
    with open(os.path.join(a.out, "quality.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    # contact sheet: input | old | new, one row per pair
    W = 384; sheet = Image.new("RGB", (W * 3, (W + 22) * len(rows)), "white"); d = ImageDraw.Draw(sheet)
    for r, p in enumerate(pairs):
        y = r * (W + 22); d.text((4, y + 4), f"{p['name']}:  input | old 5090 FireRed | new 4090 Qwen-Image-2.1 int8   — {p['prompt'][:90]}", fill="black")
        cells = [p["image"]] + [next((os.path.join(a.out, fn) for fn in os.listdir(a.out) if fn.startswith(f"{p['name']}_{t}.")), None) for t in ("old", "new")]
        for c, path in enumerate(cells):
            if path and os.path.exists(path):
                sheet.paste(Image.open(path).convert("RGB").resize((W, W)), (c * W, y + 22))
            else:
                d.text((c * W + 8, y + 40), "missing", fill="red")
    sheet.save(os.path.join(a.out, "quality_sheet.jpg"), quality=85)
    print("→", os.path.join(a.out, "quality.csv"), os.path.join(a.out, "quality_sheet.jpg"))


if __name__ == "__main__":
    main()
