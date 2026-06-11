# ComfyUI Setup for Chromafur Alpha (FLUX.1-dev Fine-tune)

This guide covers setting up ComfyUI to run Chromafur Alpha, a furry art fine-tune of FLUX.1-dev, on a system with 2x RTX 5090 (64GB total VRAM).

## Quick Start (TL;DR)

```bash
cd experimental/local-llm/comfyui

# 1. Download T5 encoder + FLUX VAE (~10GB)
./download-extras.sh

# 2. Start ComfyUI
./start-comfyui.sh

# 3. Open http://wyrm:8188
# 4. Load workflow from: /root/ComfyUI/user/default/workflows/chromafur.json
```

## Hardware Overview

- **GPUs**: 2x NVIDIA RTX 5090 (32GB each, 64GB total)
- **Models already downloaded**: `/wyrmhdd/huggingface/hub/`
  - `models--lodestone-horizon--chromafur-alpha/` (Chromafur Alpha - FLUX.1-dev fine-tune)
  - `models--LyliaEngine--Pony_Diffusion_V6_XL/` (Pony Diffusion V6 XL - SDXL fine-tune)

## Part 1: Installing ComfyUI

### Option A: Podman with start script (Recommended)

Uses [yanwk/comfyui-boot:cu128-slim](https://hub.docker.com/r/yanwk/comfyui-boot) - CUDA 12.8 for RTX 5090.

```bash
# One-time setup: download T5 encoder and FLUX VAE
./download-extras.sh

# Start ComfyUI (sets up symlinks, starts container)
./start-comfyui.sh
```

### Option B: Manual Podman run

```bash
podman run -it --rm \
  --name comfyui \
  --device nvidia.com/gpu=all \
  -p 8188:8188 \
  -v /wyrmhdd/comfyui/models:/root/ComfyUI/models \
  -v /wyrmhdd/comfyui/output:/root/ComfyUI/output \
  -v /wyrmhdd/comfyui/input:/root/ComfyUI/input \
  -v /wyrmhdd/comfyui/custom_nodes:/root/ComfyUI/custom_nodes \
  -v /wyrmhdd/huggingface/hub:/root/.cache/huggingface/hub:ro \
  docker.io/yanwk/comfyui-boot:cu128-slim
```

### Option C: Native Installation (pip)

```bash
# Create a virtual environment
cd /wyrmhdd
python3 -m venv comfyui-venv
source comfyui-venv/bin/activate

# Clone ComfyUI
git clone https://github.com/comfyanonymous/ComfyUI.git /wyrmhdd/ComfyUI
cd /wyrmhdd/ComfyUI

# Install PyTorch with CUDA 12.8+ support (required for RTX 5090)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# Install ComfyUI dependencies
pip install -r requirements.txt

# Run ComfyUI
python main.py --listen 0.0.0.0 --port 8188
```

## Part 2: Configuring Model Paths

Create `/wyrmhdd/comfyui/extra_model_paths.yaml` (or in ComfyUI root for native install):

```yaml
# Custom model paths for HuggingFace hub models
huggingface:
  base_path: /wyrmhdd/huggingface/hub

  # Chromafur Alpha (FLUX.1-dev fine-tune)
  # Note: These use the HuggingFace cache structure with symlinks
  diffusion_models: |
    models--lodestone-horizon--chromafur-alpha/snapshots/fd65eabc28536705605059f5caef2a947af8fd0e

  clip: |
    models--lodestone-horizon--chromafur-alpha/snapshots/fd65eabc28536705605059f5caef2a947af8fd0e

  # Pony Diffusion V6 XL (SDXL checkpoint)
  checkpoints: |
    models--LyliaEngine--Pony_Diffusion_V6_XL/snapshots/14885f1c01d7723bcbf676773def286c1fd733bc

  vae: |
    models--LyliaEngine--Pony_Diffusion_V6_XL/snapshots/14885f1c01d7723bcbf676773def286c1fd733bc

# Alternative: Create symlinks in standard ComfyUI directories
# This is often more reliable than extra_model_paths.yaml
```

### Alternative: Symlink Approach (More Reliable)

Instead of using `extra_model_paths.yaml`, create symlinks to the HuggingFace models:

```bash
COMFYUI_DIR=/wyrmhdd/ComfyUI  # Adjust as needed
HF_HUB=/wyrmhdd/huggingface/hub

# Create model directories
mkdir -p $COMFYUI_DIR/models/{unet,clip,vae,checkpoints}

# Chromafur Alpha - UNET/Diffusion Model
ln -s "$HF_HUB/models--lodestone-horizon--chromafur-alpha/snapshots/fd65eabc28536705605059f5caef2a947af8fd0e/chromafur-alpha_model.safetensors" \
  "$COMFYUI_DIR/models/unet/chromafur-alpha_model.safetensors"

# Chromafur Alpha - Custom CLIP
ln -s "$HF_HUB/models--lodestone-horizon--chromafur-alpha/snapshots/fd65eabc28536705605059f5caef2a947af8fd0e/chromafur-alpha_CLIP_L.safetensors" \
  "$COMFYUI_DIR/models/clip/chromafur-alpha_CLIP_L.safetensors"

# Pony Diffusion V6 XL - SDXL Checkpoint
ln -s "$HF_HUB/models--LyliaEngine--Pony_Diffusion_V6_XL/snapshots/14885f1c01d7723bcbf676773def286c1fd733bc/ponyDiffusionV6XL_v6StartWithThisOne.safetensors" \
  "$COMFYUI_DIR/models/checkpoints/ponyDiffusionV6XL.safetensors"

# Pony Diffusion V6 XL - VAE (can also be used for SDXL models)
ln -s "$HF_HUB/models--LyliaEngine--Pony_Diffusion_V6_XL/snapshots/14885f1c01d7723bcbf676773def286c1fd733bc/sdxl_vae.safetensors" \
  "$COMFYUI_DIR/models/vae/sdxl_vae.safetensors"
```

## Part 3: Additional Required Downloads

Chromafur Alpha requires T5 text encoder and FLUX VAE that are NOT included in the HuggingFace download:

### T5 Text Encoder

```bash
# Download T5 (choose one based on VRAM - you have plenty, use fp16)
cd $COMFYUI_DIR/models/clip

# FP16 version (9.79 GB) - highest quality, recommended for 64GB VRAM
wget https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp16.safetensors

# OR FP8 version (4.89 GB) - if you want to save VRAM
# wget https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors
```

### FLUX VAE

```bash
cd $COMFYUI_DIR/models/vae

# FLUX VAE (335 MB)
wget https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/ae.safetensors
```

### CLIP-L (if not using Chromafur's custom CLIP)

```bash
cd $COMFYUI_DIR/models/clip

# Standard CLIP-L for FLUX (if needed as fallback)
wget https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
```

## Part 4: Custom Nodes

ComfyUI has **native FLUX support** since August 2024. However, these custom nodes enhance the experience:

### Essential: ComfyUI-Manager

```bash
cd $COMFYUI_DIR/custom_nodes
git clone https://github.com/ltdrdata/ComfyUI-Manager.git
```

After installing, restart ComfyUI and use Manager to install other nodes easily.

### Optional but Recommended

Install via ComfyUI-Manager after it's set up:

1. **ComfyUI-GGUF** - If you want to use the GGUF quantized versions of Chromafur (for lower VRAM usage, though you have plenty)
2. **ComfyUI Essentials** - Quality of life improvements
3. **x-flux-comfyui** - XLabs FLUX extensions (LoRA, ControlNet support)

## Part 5: Chromafur Alpha Workflow

The model includes a ComfyUI workflow at:

```
/wyrmhdd/huggingface/hub/models--lodestone-horizon--chromafur-alpha/snapshots/fd65eabc28536705605059f5caef2a947af8fd0e/comfy-workflow.json
```

### Loading the Workflow

1. Open ComfyUI in browser: `http://localhost:8188`
2. Drag and drop `comfy-workflow.json` into the interface
3. Configure the model paths in the nodes

### Manual Workflow Setup

If you prefer to build from scratch, here are the key nodes:

```
[Load Diffusion Model] → chromafur-alpha_model.safetensors (from unet folder)
        ↓
[DualCLIPLoader] → chromafur-alpha_CLIP_L.safetensors + t5xxl_fp16.safetensors
        ↓
[CLIP Text Encode] × 2 (positive + negative)
        ↓
[KSampler] or [BasicGuider] + [SamplerCustomAdvanced]
        ↓
[VAE Decode] → ae.safetensors
        ↓
[Save Image]
```

### Recommended Settings (from Chromafur docs)

| Setting        | Value     | Notes                                   |
| -------------- | --------- | --------------------------------------- |
| Resolution     | 1024x1024 | Quality degrades at non-square ratios   |
| Steps          | 15-30     | 15 for fast, 30 for quality             |
| CFG            | 1 or 2-4  | CFG 1 = faster, CFG 2-4 = more coherent |
| Guidance       | 4         | FLUX-specific guidance scale            |
| CFG Skip Steps | 4         | **Critical when using CFG 2-4**         |
| Sampler        | euler     | Default from workflow                   |

### Prompting Format

**CLIP Box (tags):**

```
image tags: anthro, wolf, male, blue fur, detailed, masterpiece
```

**T5 Box (natural language + tags):**

```
image tags: anthro, wolf, male, blue fur, detailed, masterpiece
image captions: "A majestic anthropomorphic wolf with vibrant blue fur, standing confidently in a forest clearing. The character has expressive amber eyes and detailed musculature."
```

**Negatives** (only work with CFG > 1):

```
image tags: human, blurry, low quality, watermark, text
```

## Part 6: Dual RTX 5090 Optimization

### ComfyUI GPU Selection

ComfyUI doesn't automatically parallelize across GPUs, but you can:

1. **Use one GPU for generation** (default behavior - uses GPU 0)
2. **Specify GPU** via command line: `python main.py --cuda-device 1`
3. **Run multiple ComfyUI instances** on different ports/GPUs for parallel batch rendering

### Running Two Instances (Podman)

```bash
# Terminal 1 - GPU 0
podman run -it --rm --name comfyui-gpu0 \
  --device nvidia.com/gpu=0 -p 8188:8188 \
  -v /wyrmhdd/comfyui/models:/root/ComfyUI/models \
  -v /wyrmhdd/comfyui/output:/root/ComfyUI/output \
  docker.io/yanwk/comfyui-boot:cu128-slim

# Terminal 2 - GPU 1
podman run -it --rm --name comfyui-gpu1 \
  --device nvidia.com/gpu=1 -p 8189:8188 \
  -v /wyrmhdd/comfyui/models:/root/ComfyUI/models \
  -v /wyrmhdd/comfyui/output:/root/ComfyUI/output \
  docker.io/yanwk/comfyui-boot:cu128-slim
```

### Memory Optimization (Not Needed with 64GB)

With 64GB total VRAM, you can:

- Use FP16 models without quantization
- Run larger batch sizes
- Enable model caching for faster subsequent generations
- Load multiple models simultaneously

In ComfyUI settings, you can leave memory optimization disabled since you have abundant VRAM.

## Part 7: GGUF Quantized Models (Optional)

Your Chromafur download includes GGUF quantized versions for lower VRAM usage:

| File                              | Size    | Use Case                   |
| --------------------------------- | ------- | -------------------------- |
| chromafur-alpha_model-BF16.gguf   | 24 GB   | Full quality               |
| chromafur-alpha_model-Q8_0.gguf   | ~13 GB  | Near-lossless              |
| chromafur-alpha_model-Q6_K.gguf   | ~10 GB  | Good quality               |
| chromafur-alpha_model-Q5_K_S.gguf | ~8.5 GB | Balanced                   |
| chromafur-alpha_model-Q4_K_S.gguf | ~7 GB   | Lower quality, much faster |

To use GGUF models:

1. Install ComfyUI-GGUF via Manager
2. Use the GGUF Loader node instead of standard diffusion model loader
3. Point to files in: `/wyrmhdd/huggingface/hub/models--lodestone-horizon--chromafur-alpha/snapshots/.../GGUF/`

**Note**: With 64GB VRAM, you don't need GGUF - use the full `chromafur-alpha_model.safetensors` for best quality.

## Quick Start Checklist

1. [x] Install ComfyUI (Podman or native)
2. [x] Create symlinks or configure `extra_model_paths.yaml`
3. [x] Download T5 encoder (`t5xxl_fp16.safetensors`)
4. [x] Download FLUX VAE (`ae.safetensors`)
5. [x] Install ComfyUI-Manager
6. [x] Load the included workflow (`comfy-workflow.json`)
7. [x] Configure model paths in workflow nodes
8. [x] Generate!

## File Locations Summary

| Component          | Path                                                                                                                                |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| Chromafur UNET     | `/wyrmhdd/huggingface/hub/models--lodestone-horizon--chromafur-alpha/snapshots/.../chromafur-alpha_model.safetensors`               |
| Chromafur CLIP     | `/wyrmhdd/huggingface/hub/models--lodestone-horizon--chromafur-alpha/snapshots/.../chromafur-alpha_CLIP_L.safetensors`              |
| Chromafur Workflow | `/wyrmhdd/huggingface/hub/models--lodestone-horizon--chromafur-alpha/snapshots/.../comfy-workflow.json`                             |
| T5 Encoder         | Download to `$COMFYUI_DIR/models/clip/t5xxl_fp16.safetensors`                                                                       |
| FLUX VAE           | Download to `$COMFYUI_DIR/models/vae/ae.safetensors`                                                                                |
| Pony Diffusion     | `/wyrmhdd/huggingface/hub/models--LyliaEngine--Pony_Diffusion_V6_XL/snapshots/.../ponyDiffusionV6XL_v6StartWithThisOne.safetensors` |

## Sources

- [Chromafur Alpha on HuggingFace](https://huggingface.co/lodestone-horizon/chromafur-alpha)
- [Chromafur Alpha ComfyUI Workflow on Civitai](https://civitai.com/models/734928/chromafur-alpha-comfyui-workflow)
- [ComfyUI FLUX Examples](https://comfyanonymous.github.io/ComfyUI_examples/flux/)
- [ComfyUI Documentation](https://docs.comfy.org/tutorials/flux/flux-1-text-to-image)
- [ComfyUI-Nvidia-Docker](https://github.com/mmartial/ComfyUI-Nvidia-Docker) (CUDA 12.8+ for RTX 5090)
- [HOSTKEY Dual RTX 5090 Testing](https://huggingface.co/blog/HOSTKEY/more-5090-more-problems-testing-a-dual-nvidia-gpu)
- [ComfyUI FLUX Setup Guide](https://comfyui-wiki.com/en/tutorial/advanced/image/flux/flux-1-dev-t2i)
- [Flux Text Encoders](https://huggingface.co/comfyanonymous/flux_text_encoders)
- [ComfyUI extra_model_paths.yaml](https://github.com/comfyanonymous/ComfyUI/blob/master/extra_model_paths.yaml.example)
