#!/usr/bin/env bash
# Start ComfyUI with Chromafur Alpha on wyrm
# Uses yanwk/comfyui-boot:cu128-slim (CUDA 12.8 for RTX 5090)
set -euo pipefail

COMFYUI_DIR="/wyrmhdd/comfyui"
HF_HUB="/wyrmhdd/huggingface/hub"

echo "=== ComfyUI Startup ==="

# Check if /wyrmhdd is writable
if ! touch "$COMFYUI_DIR/.write-test" 2>/dev/null; then
  echo "ERROR: $COMFYUI_DIR is not writable (filesystem may be mounted read-only)"
  echo "Fix with: sudo mount -o remount,rw /wyrmhdd"
  exit 1
fi
rm -f "$COMFYUI_DIR/.write-test"

# Create directories
mkdir -p "$COMFYUI_DIR"/{models/{unet,clip,vae,checkpoints,loras},output,input,custom_nodes,workflows}

# Find Chromafur snapshot
CHROMAFUR_SNAP=$(ls -d "$HF_HUB"/models--lodestone-horizon--chromafur-alpha/snapshots/*/ 2>/dev/null | head -1)
if [[ -z "$CHROMAFUR_SNAP" ]]; then
  echo "ERROR: Chromafur Alpha not found. Download with:"
  echo "  huggingface-cli download lodestone-horizon/chromafur-alpha"
  exit 1
fi

# Setup symlinks (idempotent)
echo "Setting up model symlinks..."

# Helper: create symlink only if missing or pointing to wrong target
link_model() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] || return 0
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    return 0 # Already correct
  fi
  ln -sf "$src" "$dst"
}

# Chromafur UNET
link_model "${CHROMAFUR_SNAP}chromafur-alpha_model.safetensors" "$COMFYUI_DIR/models/unet/chromafur-alpha_model.safetensors"

# Chromafur CLIP
link_model "${CHROMAFUR_SNAP}chromafur-alpha_CLIP_L.safetensors" "$COMFYUI_DIR/models/clip/chromafur-alpha_CLIP_L.safetensors"

# Copy workflow if not exists
[[ -f "${CHROMAFUR_SNAP}comfy-workflow.json" && ! -f "$COMFYUI_DIR/workflows/chromafur.json" ]] \
  && cp "${CHROMAFUR_SNAP}comfy-workflow.json" "$COMFYUI_DIR/workflows/chromafur.json"

# Pony Diffusion (if exists)
PONY_SNAP=$(ls -d "$HF_HUB"/models--LyliaEngine--Pony_Diffusion_V6_XL/snapshots/*/ 2>/dev/null | head -1)
if [[ -n "$PONY_SNAP" ]]; then
  link_model "${PONY_SNAP}ponyDiffusionV6XL_v6StartWithThisOne.safetensors" "$COMFYUI_DIR/models/checkpoints/ponyDiffusionV6XL_v6StartWithThisOne.safetensors"
  link_model "${PONY_SNAP}sdxl_vae.safetensors" "$COMFYUI_DIR/models/vae/sdxl_vae.safetensors"
fi

# Check for required files (T5 + FLUX VAE)
MISSING=""
[[ ! -f "$COMFYUI_DIR/models/clip/t5xxl_fp16.safetensors" ]] && MISSING="T5 encoder"
[[ ! -f "$COMFYUI_DIR/models/vae/ae.safetensors" ]] && MISSING="$MISSING, FLUX VAE"

if [[ -n "$MISSING" ]]; then
  echo ""
  echo "Missing required files: $MISSING"
  echo "Run ./download-extras.sh first, or download manually:"
  echo "  T5:  wget -O $COMFYUI_DIR/models/clip/t5xxl_fp16.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp16.safetensors"
  echo "  VAE: wget -O $COMFYUI_DIR/models/vae/ae.safetensors https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/ae.safetensors"
  echo ""
  read -p "Continue anyway? [y/N] " -n 1 -r
  echo
  [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
fi

echo "Starting ComfyUI container..."
exec podman run -it --rm \
  --name comfyui \
  --device nvidia.com/gpu=all \
  -p 8188:8188 \
  -v "$COMFYUI_DIR/models:/root/ComfyUI/models" \
  -v "$COMFYUI_DIR/output:/root/ComfyUI/output" \
  -v "$COMFYUI_DIR/input:/root/ComfyUI/input" \
  -v "$COMFYUI_DIR/custom_nodes:/root/ComfyUI/custom_nodes" \
  -v "$COMFYUI_DIR/workflows:/root/ComfyUI/user/default/workflows" \
  -v "$HF_HUB:/root/.cache/huggingface/hub:ro" \
  -v "$HF_HUB:$HF_HUB:ro" \
  docker.io/yanwk/comfyui-boot:cu128-slim
