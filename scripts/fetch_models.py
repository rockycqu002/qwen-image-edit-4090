#!/usr/bin/env python3
"""Download Qwen-Image-2.1 weight files at a pinned Hugging Face revision and verify their SHA-256.

Called once per file from the Dockerfile so that every weight lands in its own image layer (GHCR caps layers at
10 GB). The expected hashes were computed on the validated AutoDL environment (2026-09-25); a mismatch fails the
build. `--manifest` (on the last call) writes <dest>/MANIFEST.json from the per-file records so the worker can log
exactly which weights it serves.
"""
import argparse, hashlib, json, os, shutil, sys, time

from huggingface_hub import hf_hub_download

FILES = {
    "diffusion_models/qwen_image_2.1_int8_convrot.safetensors": "cb74113cb03faecd79611b01fd7fd642f0aa60d6f0b95086abee214d75eaa57d",
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors": "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f",
    "vae/qwen_image_2.1_vae_bf16.safetensors": "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9",
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(repo, revision, dest, rel):
    expected = FILES[rel]
    t0 = time.time()
    path = hf_hub_download(repo, rel, revision=revision, local_dir=dest)
    final = os.path.join(dest, rel)
    if os.path.realpath(path) != os.path.realpath(final):
        os.makedirs(os.path.dirname(final), exist_ok=True); shutil.move(path, final)
    got, size = sha256(final), os.path.getsize(final)
    print(f"{rel}: {size / 1e9:.2f} GB sha256={got} ({time.time() - t0:.0f}s)", flush=True)
    if got != expected:
        print(f"SHA-256 MISMATCH for {rel}: expected {expected}", file=sys.stderr); sys.exit(1)
    shutil.rmtree(os.path.join(dest, ".cache"), ignore_errors=True)
    rec = {"path": rel, "bytes": size, "sha256": got}
    os.makedirs(os.path.join(dest, ".records"), exist_ok=True)
    with open(os.path.join(dest, ".records", rel.replace("/", "__") + ".json"), "w") as f:
        json.dump(rec, f)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True); ap.add_argument("--revision", required=True); ap.add_argument("--dest", required=True)
    ap.add_argument("--only", action="append", help="relative file path(s) to fetch; default all three")
    ap.add_argument("--manifest", action="store_true", help="write MANIFEST.json from all per-file records")
    a = ap.parse_args()
    for rel in (a.only or list(FILES)):
        if rel not in FILES:
            sys.exit(f"unknown file {rel}")
        fetch(a.repo, a.revision, a.dest, rel)
    if a.manifest:
        recdir = os.path.join(a.dest, ".records")
        files = [json.load(open(os.path.join(recdir, f))) for f in sorted(os.listdir(recdir))]
        missing = set(FILES) - {r["path"] for r in files}
        if missing:
            sys.exit(f"manifest incomplete, missing {missing}")
        with open(os.path.join(a.dest, "MANIFEST.json"), "w") as f:
            json.dump({"repo": a.repo, "revision": a.revision, "files": files}, f, indent=1)
        shutil.rmtree(recdir, ignore_errors=True)
        print("manifest written", flush=True)


if __name__ == "__main__":
    main()
