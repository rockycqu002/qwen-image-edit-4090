# syntax=docker/dockerfile:1.7
# Qwen-Image-2.1 (int8 convrot) image-edit worker for RunPod Serverless on RTX 4090 (CUDA 13 / driver >= 580).
# Everything is pinned: base image by digest, ComfyUI by commit, Python packages by constraints (frozen from the
# validated AutoDL environment), model weights by HF revision + sha256 (checked in scripts/fetch_models.py).
#
# torch's PyPI wheels bring their own CUDA 13 runtime libraries (nvidia-*-cu13) and Triton bundles ptxas, so the
# slim "-base" CUDA image is enough. Each weight file gets its own layer: GHCR rejects layers above 10 GB.
FROM nvidia/cuda:13.0.1-base-ubuntu24.04@sha256:f8ef28f579ea42a44b415d2c5d46f788e6a9b395c6c83f2929416e1fc192c143 AS runtime

ARG COMFY_COMMIT=88ab4a06566454ad89db8f0bedb970d6c08cd1b7
ARG HF_REPO=Comfy-Org/Qwen-Image-2.1
ARG HF_REVISION=9a44dbdb47cefd046be9c0a13476192f34c8db8e

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
    COMFY_DIR=/app/ComfyUI RUNPOD_LOG_LEVEL=WARN

# gcc + libc6-dev + python3.12-dev: Triton JIT-compiles the int8 / SageAttention kernels at runtime and needs a C compiler
# and Python.h ("Failed to find C compiler" killed every worker on the -base image without them).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev gcc libc6-dev git tini ca-certificates curl libgl1 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/* \
    && python3.12 -m venv /opt/venv && pip install "pip==25.3"

WORKDIR /app
COPY constraints.txt ./

# torch 2.14.0 on PyPI is the CUDA 13.0 build
RUN pip install -c constraints.txt torch==2.14.0 torchvision==0.29.0 torchaudio==2.11.0

# ComfyUI at the validated commit, without git history; its optional template gallery package is skipped
RUN git init -q ComfyUI && cd ComfyUI \
    && git remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git fetch -q --depth 1 origin ${COMFY_COMMIT} && git checkout -q FETCH_HEAD && rm -rf .git \
    && grep -v '^comfyui-workflow-templates' requirements.txt > /tmp/comfy-req.txt \
    && pip install -c /app/constraints.txt -r /tmp/comfy-req.txt

COPY requirements.txt ./
RUN pip install -c constraints.txt -r requirements.txt

# model weights: pinned revision, sha256-verified, one layer per file (7.3 GB / 9.4 GB / 0.7 GB)
COPY scripts/fetch_models.py scripts/
RUN HF_HOME=/tmp/hf python scripts/fetch_models.py --repo ${HF_REPO} --revision ${HF_REVISION} --dest /models --only diffusion_models/qwen_image_2.1_int8_convrot.safetensors && rm -rf /tmp/hf
RUN HF_HOME=/tmp/hf python scripts/fetch_models.py --repo ${HF_REPO} --revision ${HF_REVISION} --dest /models --only text_encoders/qwen3vl_8b_int8_convrot.safetensors && rm -rf /tmp/hf
RUN HF_HOME=/tmp/hf python scripts/fetch_models.py --repo ${HF_REPO} --revision ${HF_REVISION} --dest /models --only vae/qwen_image_2.1_vae_bf16.safetensors --manifest && rm -rf /tmp/hf

COPY comfy/extra_model_paths.yaml ComfyUI/extra_model_paths.yaml
COPY src/ /app/src/

RUN pip freeze > /app/requirements.lock \
    && python - <<'EOF'
import torch, importlib.metadata as m
print("torch", torch.__version__, "cuda", torch.version.cuda, "| sageattention", m.version("sageattention"),
      "| comfy-kitchen", m.version("comfy-kitchen"), "| runpod", m.version("runpod"))
assert torch.version.cuda.startswith("13."), torch.version.cuda
import json; mf = json.load(open("/models/MANIFEST.json")); assert len(mf["files"]) == 3, mf
EOF

# last, so a new tag only rebuilds this layer: image tag echoed in info.build; QIE_COMFY_ARGS = extra ComfyUI flags
# (override per template release, e.g. "--disable-dynamic-vram")
ARG BUILD_REF=dev
ENV QIE_BUILD=${BUILD_REF} QIE_COMFY_ARGS=""

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/src/handler.py"]

# tiny stage used by CI to pull the lock file and manifest out of the image without re-pulling 28 GB
FROM scratch AS artifacts
COPY --from=runtime /app/requirements.lock /requirements.lock
COPY --from=runtime /models/MANIFEST.json /MANIFEST.json
