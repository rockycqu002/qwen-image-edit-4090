#!/usr/bin/env python3
"""Cold-start, hot-run, concurrency and burst measurements against a live endpoint (requirement §6.3, §6.4).

  python tests/run_load.py --endpoint <id> hot --n 30
  python tests/run_load.py --endpoint <id> cold --n 5 --settle 300     # wait until 0 workers between requests
  python tests/run_load.py --endpoint <id> concurrency --levels 1 3 5
  python tests/run_load.py --endpoint <id> burst --n 7 --window 60

Every job is appended to results/load.csv with: mode, job id, status, delayTime, executionTime, wall, output bytes,
seed, and (for cold runs) how long the endpoint sat at zero workers before the request.
"""
import argparse, base64, csv, io, os, sys, threading, time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
import rp  # noqa: E402

PROMPTS = ["Change the background to a plain light blue wall. Keep the subject unchanged.",
           "Make the box matte dark green. Keep everything else the same.",
           "Turn this into a watercolor painting, keeping the composition."]


def image_b64(i):
    im = Image.new("RGB", (1024, 768), (235, 228, 210)); d = ImageDraw.Draw(im)
    d.rectangle((300, 260, 720, 700), fill=(40 + i * 7 % 100, 90, 160)); d.ellipse((380, 90, 640, 330), fill=(230, 190, 150))
    d.text((10, 10), f"load test {i}", fill=(0, 0, 0))
    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=92); return base64.b64encode(buf.getvalue()).decode()


def one(endpoint, i, mode, extra=None):
    t0 = time.time()
    sub = rp.run(endpoint, {"image": image_b64(i), "prompt": PROMPTS[i % len(PROMPTS)]})
    if "id" not in sub:
        return {"mode": mode, "i": i, "status": f"HTTP {sub.get('submit_error')}", "error": sub.get("body", "")[:200], **(extra or {})}
    doc = rp.wait(endpoint, sub["id"])
    row = {"mode": mode, "i": i, "job_id": sub["id"], "status": doc.get("status"), "submit_ms": sub["submit_ms"], "delay_ms": doc.get("delayTime"),
           "exec_ms": doc.get("executionTime"), "wall_ms": doc.get("wall_ms"), "e2e_ms": int((time.time() - t0) * 1000), "error": str(doc.get("error", ""))[:160], **(extra or {})}
    if doc.get("status") == "COMPLETED":
        try:
            im, fmt, n = rp.decode_output(doc); info = (doc.get("output") or {}).get("info") or {}
            row.update(out_bytes=n, out_size=f"{im.width}x{im.height}", seed=info.get("seed"), infer_ms=info.get("infer_ms"), gpu=info.get("gpu"))
        except Exception as e:
            row.update(status="COMPLETED_BAD_OUTPUT", error=repr(e)[:160])
    return row


def workers(endpoint):
    h = rp.health(endpoint); w = h.get("workers", {})
    return sum(int(w.get(k, 0)) for k in ("idle", "running", "initializing", "ready"))


def append(path, rows):
    new = not os.path.exists(path)
    keys = ["mode", "i", "job_id", "status", "submit_ms", "delay_ms", "exec_ms", "wall_ms", "e2e_ms", "out_bytes", "out_size", "seed", "infer_ms", "gpu", "zero_workers_s", "error"]
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore"); w.writeheader() if new else None; w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--endpoint", required=True); ap.add_argument("--out", default="results/load.csv")
    sub = ap.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("hot"); h.add_argument("--n", type=int, default=30)
    c = sub.add_parser("cold"); c.add_argument("--n", type=int, default=5); c.add_argument("--settle", type=int, default=300, help="max seconds to wait for 0 workers")
    k = sub.add_parser("concurrency"); k.add_argument("--levels", type=int, nargs="+", default=[1, 3, 5])
    b = sub.add_parser("burst"); b.add_argument("--n", type=int, default=7); b.add_argument("--window", type=int, default=60)
    a = ap.parse_args(); os.makedirs(os.path.dirname(a.out), exist_ok=True)
    rows = []
    if a.mode == "hot":
        for i in range(a.n):
            rows.append(one(a.endpoint, i, "hot")); print(rows[-1], flush=True)
    elif a.mode == "cold":
        for i in range(a.n):
            t0 = time.time()
            while workers(a.endpoint) > 0 and time.time() - t0 < a.settle:
                time.sleep(5)
            zero_for = 0; t1 = time.time()
            while time.time() - t1 < 20:                      # give the platform a moment at zero
                time.sleep(5); zero_for = int(time.time() - t1)
            rows.append(one(a.endpoint, 100 + i, "cold", {"zero_workers_s": zero_for})); print(rows[-1], flush=True)
    elif a.mode == "concurrency":
        for lvl in a.levels:
            out, th = [], []
            for j in range(lvl):
                t = threading.Thread(target=lambda j=j: out.append(one(a.endpoint, 200 + lvl * 10 + j, f"conc{lvl}"))); t.start(); th.append(t)
            for t in th: t.join()
            rows += out; [print(r, flush=True) for r in out]
    elif a.mode == "burst":
        out, th, gap = [], [], a.window / a.n
        for j in range(a.n):
            t = threading.Thread(target=lambda j=j: out.append(one(a.endpoint, 300 + j, "burst"))); t.start(); th.append(t); time.sleep(gap)
        for t in th: t.join()
        rows += out; [print(r, flush=True) for r in out]
    append(a.out, rows)
    ok = [r for r in rows if r.get("status") == "COMPLETED"]
    if ok:
        import statistics as st
        e2e = sorted(r["e2e_ms"] for r in ok); ex = sorted(r["exec_ms"] or 0 for r in ok); dl = sorted(r["delay_ms"] or 0 for r in ok)
        pct = lambda v, p: v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
        print(f"\n{a.mode}: {len(ok)}/{len(rows)} completed | e2e mean {st.mean(e2e)/1000:.1f}s P50 {pct(e2e,.5)/1000:.1f}s P95 {pct(e2e,.95)/1000:.1f}s | "
              f"exec mean {st.mean(ex)/1000:.1f}s | delay mean {st.mean(dl)/1000:.1f}s P95 {pct(dl,.95)/1000:.1f}s → {a.out}")


if __name__ == "__main__":
    main()
