# qwen-image-edit-4090

RunPod Serverless worker: **Qwen-Image-2.1** single-image editing with the Comfy-Org **int8 convrot** weights
(transformer + Qwen3-VL-8B text encoder int8, VAE bf16) on **RTX 4090 / CUDA 13**, served through ComfyUI.

Protocol-compatible with the previous FireRed-Edit endpoint:

```json
POST https://api.runpod.ai/v2/<ENDPOINT_ID>/run
{"input": {"image": "<RAW_BASE64>", "prompt": "<instruction>"}}
→ status: {"output": {"image": "<JPEG q95 BASE64>", "info": {"seed": 123, "steps": 20, "resolution": 1024, "width": 1184, "height": 896, ...}}}
```

Optional inputs (omit for defaults): `ref_image` (2nd reference), `ref_images` (list, up to 6 images total),
`steps` 4–30 (default 20), `resolution` 768–1280 (default 1024, area-based edge length), `seed` (default random).
Output container is always JPEG q95; input PNG/WebP transparency is composited onto white; EXIF orientation is
applied; only the first frame of animated inputs is used.

## Layout

| path | purpose |
|---|---|
| `Dockerfile` | CUDA 13.0.1 base (digest-pinned) → torch 2.14.0+cu130 → ComfyUI @ `88ab4a0` → weights baked at `/models` |
| `constraints.txt` | pip pins frozen from the validated AutoDL environment |
| `scripts/fetch_models.py` | downloads the 3 weight files at HF revision `9a44dbdb` and verifies SHA-256 |
| `comfy/extra_model_paths.yaml` | tells ComfyUI where the baked weights live |
| `src/handler.py` | RunPod handler: starts ComfyUI, warms up at 1024², validates/pre-processes input, runs the graph, encodes JPEG |
| `.github/workflows/build.yml` | builds and pushes `ghcr.io/<owner>/qwen-image-edit-4090:<tag>` on `v*` tags; uploads `requirements.lock` + `MANIFEST.json` |
| `tests/` | CPU unit tests + live protocol / load / quality scripts (`tests/rp.py` is the API client) |

## Build & release

```
git tag v0.1.0 && git push --tags      # GitHub Actions builds (~30–40 min) and pushes to GHCR
```
The workflow summary shows the image digest, the model manifest and the lock file.

A package first published from Actions is **private**. Either make it public once (GitHub → Packages →
`qwen-image-edit-4090` → Package settings → Change visibility) or register a GHCR pull credential in RunPod
(Credentials → Container Registry Auth) and pass its id to `deploy/runpod_api.py create --registry-auth <id>`.
Use the digest-pinned reference `ghcr.io/<owner>/qwen-image-edit-4090:v0.1.0@sha256:...` for the template.

## Endpoint settings (RunPod)

Queue-based · GPU `NVIDIA GeForce RTX 4090` only (pool ADA_24, minCudaVersion 13.0) · 1 GPU/worker ·
workersMin 0 · idleTimeout 5 s · scaler REQUEST_COUNT/1 · FlashBoot on · executionTimeout 60 s ·
env `QIE_STEPS=20 QIE_RESOLUTION=1024` · **container disk 50 GB**.

The container disk must hold the *uncompressed* image (~28 GB) plus `/tmp` scratch. With the 20 GB default every
worker pulled the image, logged `start container` and died without a single line of container output (worker
`unhealthy`, job stuck `IN_QUEUE`); a diagnostics-only probe on the same hosts (US-CA-2, driver 580.126.20 /
CUDA 13.0, torch 2.14.0+cu130 → `cuda.is_available() == True`) succeeded as soon as the disk was 60 GB.

## Local checks

```
python -m venv .venv && .venv/bin/pip install pillow pytest
.venv/bin/python -m pytest tests/test_handler_unit.py -q
```
