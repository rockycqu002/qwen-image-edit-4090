# syntax=docker/dockerfile:1.7
# Qwen-Image-2.1 (int8 convrot) image-edit worker for RunPod Serverless on RTX 4090 (CUDA 13 / driver >= 580).
# Everything is pinned: base image by digest, ComfyUI by commit, Python packages by constraints (frozen from the
# validated AutoDL environment), model weights by HF revision + sha256 (checked in scripts/fetch_models.py).
FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:c3fde347d52d578c84fd644bc177bc7ec333feaf11550d990da4084d7612e4c7

ARG COMFY_COMMIT=88ab4a06566454ad89db8f0bedb970d6c08cd1b7
ARG HF_REPO=Comfy-Org/Qwen-Image-2.1
ARG HF_REVISION=9a44dbdb47cefd046be9c0a13476192f34c8db8e

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH HF_HUB_DISABLE_TELEMETRY=1 COMFY_DIR=/app/ComfyUI

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3-pip git tini ca-certificates curl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && python3.12 -m venv /opt/venv && pip install --upgrade pip

WORKDIR /app
COPY constraints.txt requirements.txt ./

# torch 2.14.0 on PyPI is the CUDA 13.0 build (bundles the nvidia-*-cu13 runtime wheels)
RUN pip install -c constraints.txt torch==2.14.0 torchvision==0.29.0 torchaudio==2.11.0

RUN git clone https://github.com/comfyanonymous/ComfyUI.git \
    && cd ComfyUI && git checkout --quiet ${COMFY_COMMIT} \
    && grep -v '^comfyui-workflow-templates' requirements.txt > /tmp/comfy-req.txt \
    && pip install -c /app/constraints.txt -r /tmp/comfy-req.txt \
    && pip install -c /app/constraints.txt -r /app/requirements.txt \
    && rm -rf ComfyUI/.git

# model weights: pinned revision, sha256-verified, manifest written next to them (~17.5 GB layer)
COPY scripts/fetch_models.py scripts/
RUN python scripts/fetch_models.py --repo ${HF_REPO} --revision ${HF_REVISION} --dest /models

COPY comfy/extra_model_paths.yaml ComfyUI/extra_model_paths.yaml
COPY src/ /app/src/

RUN pip freeze > /app/requirements.lock \
    && python - <<'EOF'
import torch, sageattention, importlib.metadata as m
print("torch", torch.__version__, "cuda", torch.version.cuda, "| sageattention", m.version("sageattention"),
      "| comfy-kitchen", m.version("comfy-kitchen"), "| runpod", m.version("runpod"))
assert torch.version.cuda.startswith("13."), torch.version.cuda
EOF

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/src/handler.py"]

# tiny stage used by CI to pull the lock file and manifest out of the image without re-pulling 28 GB
FROM scratch AS artifacts
COPY --from=0 /app/requirements.lock /requirements.lock
COPY --from=0 /models/MANIFEST.json /MANIFEST.json
