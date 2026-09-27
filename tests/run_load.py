#!/usr/bin/env python3
"""Cold-start, hot-run, concurrency and burst measurements against a live endpoint (requirement §6.3, §6.4).

  python tests/run_load.py --endpoint <id> hot --n 30
  python tests/run_load.py --endpoint <id> cold --n 5 --settle 300 --idle 90   # wait for 0 workers, idle a while, then request
  python tests/run_load.py --endpoint <id> concurrency --levels 1 3 5
  python tests/run_load.py --endpoint <id> burst --n 7 --window 60

Rows go to results/load.csv with mode, job id, worker id, status, delayTime, executionTime, wall, output bytes, seed,
worker-reported init_s / worker_jobs / VRAM / RSS (so cold starts and memory growth can be attributed and asserted),
and a /health snapshot before and after each concurrency / burst round.
"""
import argparse, base64, csv, io, json, os, sys, threading, time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
import rp  # noqa: E402

PROMPTS = ["Change the background to a plain light blue wall. Keep the subject unchanged.",
           "Make the box matte dark green. Keep everything else the same.",
           "Turn this into a watercolor painting, keeping the composition."]
COLS = ["mode", "i", "submitted_at", "finished_at", "job_id", "worker_id", "status", "submit_ms", "delay_ms", "exec_ms", "wall_ms", "e2e_ms",
        "out_bytes", "out_size", "seed", "infer_ms", "gpu", "init_s", "worker_jobs", "vram_used_mib", "rss_mib", "zero_workers_s", "since_last_done_s",
        "health_before", "health_after", "error"]


def image_b64(i):
    im = Image.new("RGB", (1024, 768), (235, 228, 210)); d = ImageDraw.Draw(im)
    d.rectangle((300, 260, 720, 700), fill=(40 + i * 7 % 100, 90, 160)); d.ellipse((380, 90, 640, 330), fill=(230, 190, 150))
    d.text((10, 10), f"load test {i}", fill=(0, 0, 0))
    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=92); return base64.b64encode(buf.getvalue()).decode()


def one(endpoint, i, mode, extra=None):
    t0 = time.time()
    sub = rp.run(endpoint, {"image": image_b64(i), "prompt": PROMPTS[i % len(PROMPTS)]})
    if "id" not in sub:
        return {"mode": mode, "i": i, "submitted_at": sub.get("submitted_at"), "status": f"HTTP {sub.get('submit_error')}", "error": sub.get("body", "")[:200], **(extra or {})}
    doc = rp.wait(endpoint, sub["id"])
    row = {"mode": mode, "i": i, "submitted_at": sub["submitted_at"], "submit_ms": sub["submit_ms"], "e2e_ms": int((time.time() - t0) * 1000), **rp.summarize(doc), **(extra or {})}
    if doc.get("status") == "COMPLETED":
        try:
            im, fmt, n = rp.decode_output(doc); row.update(out_bytes=n, out_size=f"{im.width}x{im.height}")
        except Exception as e:
            row.update(status="COMPLETED_BAD_OUTPUT", error=repr(e)[:160])
    return row


def health_snapshot(endpoint):
    try:
        h = rp.health(endpoint); return json.dumps({"workers": h.get("workers"), "jobs": h.get("jobs")})
    except Exception as e:
        return repr(e)[:80]


def live_workers(endpoint):
    h = rp.health(endpoint); w = h.get("workers", {})
    return sum(int(w.get(k, 0)) for k in ("idle", "running", "initializing", "ready"))


def append(path, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        if new: w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--endpoint", required=True); ap.add_argument("--out", default="results/load.csv")
    sub = ap.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("hot"); h.add_argument("--n", type=int, default=30)
    c = sub.add_parser("cold"); c.add_argument("--n", type=int, default=5); c.add_argument("--settle", type=int, default=300, help="max seconds to wait for 0 workers")
    c.add_argument("--idle", type=int, default=90, help="seconds to stay at 0 workers before requesting")
    k = sub.add_parser("concurrency"); k.add_argument("--levels", type=int, nargs="+", default=[1, 3, 5])
    b = sub.add_parser("burst"); b.add_argument("--n", type=int, default=7); b.add_argument("--window", type=int, default=60)
    a = ap.parse_args(); os.makedirs(os.path.dirname(a.out), exist_ok=True)
    rows = []
    if a.mode == "hot":
        last_done = None
        for i in range(a.n):
            r = one(a.endpoint, i, "hot", {"since_last_done_s": round(time.time() - last_done, 1) if last_done else None})
            last_done = time.time(); rows.append(r); print({k: r.get(k) for k in ("i", "status", "delay_ms", "exec_ms", "worker_id", "worker_jobs", "vram_used_mib", "rss_mib")}, flush=True)
    elif a.mode == "cold":
        last_done = time.time()
        for i in range(a.n):
            t0 = time.time()
            while live_workers(a.endpoint) > 0 and time.time() - t0 < a.settle:
                time.sleep(5)
            reached_zero = time.time()
            time.sleep(a.idle)
            r = one(a.endpoint, 100 + i, "cold", {"zero_workers_s": round(time.time() - reached_zero), "since_last_done_s": round(time.time() - last_done)})
            last_done = time.time(); rows.append(r); print({k: r.get(k) for k in ("i", "status", "delay_ms", "exec_ms", "e2e_ms", "worker_id", "init_s", "worker_jobs", "zero_workers_s")}, flush=True)
    elif a.mode in ("concurrency", "burst"):
        rounds = [(f"conc{l}", l, 0) for l in a.levels] if a.mode == "concurrency" else [("burst", a.n, a.window / a.n)]
        for tag, n, gap in rounds:
            before = health_snapshot(a.endpoint); out, th = [], []
            for j in range(n):
                t = threading.Thread(target=lambda j=j: out.append(one(a.endpoint, 200 + j, tag))); t.start(); th.append(t)
                if gap: time.sleep(gap)
            for t in th: t.join()
            after = health_snapshot(a.endpoint)
            for r in out: r.update(health_before=before, health_after=after)
            rows += out; [print({k: r.get(k) for k in ("mode", "i", "status", "delay_ms", "exec_ms", "e2e_ms", "worker_id", "worker_jobs")}, flush=True) for r in out]
            print(tag, "workers:", sorted({r.get("worker_id") for r in out}), "| health after:", after, flush=True)
    append(a.out, rows)
    ok = [r for r in rows if r.get("status") == "COMPLETED"]
    if ok:
        import statistics as st
        e2e = sorted(r["e2e_ms"] for r in ok); ex = sorted(r["exec_ms"] or 0 for r in ok); dl = sorted(r["delay_ms"] or 0 for r in ok)
        pct = lambda v, p: v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
        rss = [r["rss_mib"] for r in ok if r.get("rss_mib")]; vram = [r["vram_used_mib"] for r in ok if r.get("vram_used_mib")]
        print(f"\n{a.mode}: {len(ok)}/{len(rows)} completed | e2e mean {st.mean(e2e)/1000:.1f}s P50 {pct(e2e,.5)/1000:.1f}s P95 {pct(e2e,.95)/1000:.1f}s | "
              f"exec mean {st.mean(ex)/1000:.1f}s | delay mean {st.mean(dl)/1000:.1f}s P95 {pct(dl,.95)/1000:.1f}s"
              + (f" | RSS {min(rss)}→{max(rss)} MiB VRAM {min(vram)}→{max(vram)} MiB" if rss and vram else "") + f" → {a.out}")


if __name__ == "__main__":
    main()
