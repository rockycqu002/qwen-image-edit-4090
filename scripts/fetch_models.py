#!/usr/bin/env python3
"""Download the three Qwen-Image-2.1 weight files at a pinned Hugging Face revision and verify their SHA-256.

The expected hashes were computed on the validated AutoDL environment (2026-09-25). A mismatch fails the build.
Writes <dest>/MANIFEST.json so the running worker can log exactly which weights it serves.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True); ap.add_argument("--revision", required=True); ap.add_argument("--dest", required=True)
    a = ap.parse_args()
    manifest = {"repo": a.repo, "revision": a.revision, "files": []}
    for rel, expected in FILES.items():
        t0 = time.time()
        path = hf_hub_download(a.repo, rel, revision=a.revision, local_dir=a.dest)
        # local_dir may keep a .cache directory with metadata; the file itself lands at dest/rel
        final = os.path.join(a.dest, rel)
        if os.path.realpath(path) != os.path.realpath(final):
            os.makedirs(os.path.dirname(final), exist_ok=True); shutil.move(path, final)
        got = sha256(final)
        size = os.path.getsize(final)
        print(f"{rel}: {size / 1e9:.2f} GB sha256={got} ({time.time() - t0:.0f}s)", flush=True)
        if got != expected:
            print(f"SHA-256 MISMATCH for {rel}: expected {expected}", file=sys.stderr); sys.exit(1)
        manifest["files"].append({"path": rel, "bytes": size, "sha256": got})
    shutil.rmtree(os.path.join(a.dest, ".cache"), ignore_errors=True)
    with open(os.path.join(a.dest, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print("manifest written", flush=True)


if __name__ == "__main__":
    main()
