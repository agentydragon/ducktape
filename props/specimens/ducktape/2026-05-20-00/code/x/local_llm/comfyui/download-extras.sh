#!/usr/bin/env bash
# Download T5 encoder and FLUX VAE required for Chromafur
set -euo pipefail

COMFYUI_DIR="/wyrmhdd/comfyui"
mkdir -p "$COMFYUI_DIR/models/clip" "$COMFYUI_DIR/models/vae"

# HF_TOKEN required for gated models like FLUX.1-dev
if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "ERROR: HF_TOKEN not set. Required for downloading gated models."
  echo "Set it with: export HF_TOKEN=hf_xxx"
  exit 1
fi

echo "Downloading required files for FLUX/Chromafur..."

# T5 text encoder (9.8 GB) - public model, no auth needed
if [[ ! -f "$COMFYUI_DIR/models/clip/t5xxl_fp16.safetensors" ]]; then
  echo "Downloading T5 text encoder (9.8 GB)..."
  wget --progress=bar:force -O "$COMFYUI_DIR/models/clip/t5xxl_fp16.safetensors" \
    "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp16.safetensors"
else
  echo "T5 encoder already exists"
fi

# FLUX VAE (335 MB) - gated model, requires auth
if [[ ! -f "$COMFYUI_DIR/models/vae/ae.safetensors" ]]; then
  echo "Downloading FLUX VAE (335 MB)..."
  wget --header="Authorization: Bearer $HF_TOKEN" --progress=bar:force \
    -O "$COMFYUI_DIR/models/vae/ae.safetensors" \
    "https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/ae.safetensors"
else
  echo "FLUX VAE already exists"
fi

# CLIP-L (242 MB) - fallback/standard
if [[ ! -f "$COMFYUI_DIR/models/clip/clip_l.safetensors" ]]; then
  echo "Downloading CLIP-L (242 MB)..."
  wget --progress=bar:force -O "$COMFYUI_DIR/models/clip/clip_l.safetensors" \
    "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors"
else
  echo "CLIP-L already exists"
fi

echo ""
echo "Done! Now run: ./start-comfyui.sh"
