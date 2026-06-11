# Furry NSFW Image Generation Models - Self-Hosting Research

Research on state-of-the-art furry/anthro NSFW image generation models for self-hosting.

**Target Hardware:** 2x RTX 5090 (64GB total VRAM, 32GB per GPU)

## Executive Summary

With 2x RTX 5090s, you have exceptional hardware that can run any current image generation model at full precision. The best options are:

1. **Best Overall:** Pony Diffusion V6 XL or its derivatives (Midgard Pony, AutismMix Pony)
2. **Best for Furry Focus:** Nova Furry XL, Ratatoskr, or E621 Rising XL
3. **Best for FLUX Architecture:** Chromafur Alpha or Chroma + Furry Enhancer LoRA
4. **Best Inference Backend:** ComfyUI or SwarmUI (for multi-GPU)

---

## SDXL-Based Models (Recommended)

### Pony Diffusion V6 XL

The gold standard for stylized NSFW content including furry/anthro art.

| Property         | Value                                                                |
| ---------------- | -------------------------------------------------------------------- |
| Base             | SDXL 1.0                                                             |
| Training Data    | ~2.6M images (50% anime/cartoon, 50% furry/pony), 1:1 SFW/NSFW ratio |
| e621 Trained     | Yes (via source tags)                                                |
| VRAM (inference) | 8-12GB (easily fits 32GB)                                            |
| License          | Fair AI Public License 1.0-SD (non-commercial inference only)        |

**Download:**

- Civitai: https://civitai.com/models/257749/pony-diffusion-v6-xl
- HuggingFace: https://huggingface.co/LyliaEngine/Pony_Diffusion_V6_XL

**Key Features:**

- Special tags: `source_furry`, `source_pony`, `source_anime`, `source_cartoon`
- Rating tags: `rating_safe`, `rating_questionable`, `rating_explicit`
- Quality scoring: `score_9`, `score_8_up`, `score_7_up`, etc.
- Requires **clip skip 2** for best results

**Recommended Prompt Structure:**

```
score_9, score_8_up, score_7_up, source_furry, rating_explicit, [your prompt]
```

---

### Midgard - Pony [THL] (SDXL)

A Pony derivative specifically optimized for furry/creature art with more realistic rendering.

| Property  | Value                                             |
| --------- | ------------------------------------------------- |
| Base      | Pony Diffusion V6 XL + Ratatoskr + Furry Enhancer |
| Style     | More realistic than base Pony                     |
| Prompting | Natural language, e621 tags, or Pony-style        |
| VRAM      | 8-12GB                                            |

**Download:**

- Civitai: https://civitai.com/models/470287/midgard-pony-thl-sdxl (v3.2)

---

### Nova Furry XL

Highly-rated (5 stars, 1500+ reviews) dedicated furry checkpoint.

| Property  | Value                                                  |
| --------- | ------------------------------------------------------ |
| Base      | Illustrious XL                                         |
| Specialty | 2D/2.5D furry art with detailed furs, scales, feathers |
| Rating    | 4.99/5 stars (744+ reviews)                            |
| VRAM      | 8-12GB                                                 |

**Download:**

- Civitai: https://civitai.com/models/503815/nova-furry-xl (v15.0 for Illustrious)

**Note:** Non-commercial license for unedited generated images.

---

### E621 Rising XL

Directly trained on e621 dataset using the Dataset Rising toolchain.

| Property  | Value                                            |
| --------- | ------------------------------------------------ |
| Version   | v3.0                                             |
| Base      | SDXL                                             |
| Training  | E621 data at 1024x1024                           |
| Prompting | E621 tags (use underscores: `looking_at_viewer`) |
| VRAM      | 8-12GB                                           |

**Download:**

- Civitai: https://civitai.com/models/185901/e621-rising-xl
- Config: https://github.com/hearmeneigh/e621-rising-configs

---

### Ratatoskr

Fine-tuned for animals, creatures, and furries with a wide stylistic range.

| Property    | Value                                  |
| ----------- | -------------------------------------- |
| Versions    | SDXL and FLUX                          |
| Style Range | Drawing to photorealistic              |
| Recommended | 30+ steps, CFG 2.5+, DPM++ SDE sampler |
| VRAM        | 8-12GB (SDXL), 12-24GB (FLUX)          |

**Download:**

- SDXL: https://civitai.com/models/192854/ratatoskr-animal-creature-and-furry
- FLUX: https://civitai.com/models/681795/ratatoskr-animal-creature-and-furry-flux-dev-krea

---

### AutismMix Pony

Pony derivative that's more predictable with simpler negative prompts.

| Property | Value                                                   |
| -------- | ------------------------------------------------------- |
| Base     | Pony Diffusion V6 XL                                    |
| Goal     | More predictable outputs, less complex prompting needed |
| VRAM     | 8-12GB                                                  |

**Download:**

- Civitai: https://civitai.com/models/288584/autismmix-sdxl

**Tip:** Add `3d` to negatives for more traditional anime style.

---

## FLUX-Based Models

FLUX models are larger (12B parameters) but offer superior prompt following and image quality.

### Chromafur Alpha

Dedicated furry FLUX model by the Horizon Team.

| Property   | Value                                          |
| ---------- | ---------------------------------------------- |
| Base       | FLUX.1-dev                                     |
| Specialty  | High-quality SFW and NSFW furry artwork        |
| Captioning | Custom in-house model for natural descriptions |
| VRAM       | 24GB+ (FP16), 12-16GB (FP8/GGUF)               |

**Download:**

- HuggingFace: https://huggingface.co/lodestone-horizon/chromafur-alpha

**Setup:** Requires T5 text encoder from https://huggingface.co/comfyanonymous/flux_text_encoders

---

### Chroma (Base)

Apache 2.0 licensed uncensored FLUX derivative. Can be combined with furry LoRAs.

| Property      | Value                                             |
| ------------- | ------------------------------------------------- |
| Parameters    | 8.9B                                              |
| Base          | FLUX.1-schnell                                    |
| Training Data | 5M images (anime, furry, photos) from 20M curated |
| License       | Apache 2.0 (fully open)                           |
| VRAM          | 24GB+ (FP16), 12-16GB (FP8/GGUF)                  |

**Download:**

- HuggingFace: https://huggingface.co/lodestones/Chroma
- HD Version: https://huggingface.co/lodestones/Chroma1-HD

---

### Flux-Uncensored-V2

General NSFW FLUX model.

| Property | Value                         |
| -------- | ----------------------------- |
| Base     | FLUX.1-dev                    |
| Focus    | Uncensored content generation |

**Download:**

- HuggingFace: https://huggingface.co/enhanceaiteam/Flux-Uncensored-V2

---

## Illustrious/NoobAI-Based Models

### NoobAI-XL

Trained on complete Danbooru AND e621 datasets (~13M images).

| Property      | Value                                                                    |
| ------------- | ------------------------------------------------------------------------ |
| Base          | Illustrious XL                                                           |
| Training Data | 13M images (Danbooru + e621)                                             |
| Versions      | Noise prediction (more creative) and V-prediction (more prompt-accurate) |
| VRAM          | 8-12GB                                                                   |

**Download:**

- Civitai: https://civitai.com/models/833294/noobai-xl-nai-xl

**Rating Tags:** `general`, `sensitive`, `nsfw`, `explicit`

---

### WAI-NSFW-Illustrious-SDXL

Community-maintained Illustrious fork with NSFW focus.

| Property | Value              |
| -------- | ------------------ |
| Base     | Illustrious XL 0.1 |
| VAE      | Integrated         |
| VRAM     | 8-12GB             |

**Download:**

- Civitai: https://civitai.com/models/827184/wai-illustrious-sdxl

---

## LoRAs (Low-Rank Adaptations)

LoRAs are smaller addons (~100-500MB) that modify base models.

### Furry Enhancer

Enhances furry/anthro generation quality across multiple base models.

| Property   | Value                                                 |
| ---------- | ----------------------------------------------------- |
| Versions   | SDXL, FLUX                                            |
| Training   | Natural language + e621                               |
| Compatible | Midgard Pony, Ratatoskr, Bifrost, FenrisXL, Yggdrasil |

**Download:**

- Civitai: https://civitai.com/models/310964

---

### PDV6XL Artist Tags

Adds 1,436 e621 artists to Pony Diffusion for style control.

| Property      | Value                |
| ------------- | -------------------- |
| Artists       | 1,436                |
| Training Data | 350k+ images         |
| Target        | Pony Diffusion V6 XL |

**Download:**

- Tensor.Art: https://tensor.art/models/705021682227773760

---

### Illustrious E621++ LoRA

Adds e621 knowledge to Illustrious-based models.

**Settings:** Strength 0.8-1.0, 20-50 steps, Euler Ancestral Karras (CFG 6-8)

**Download:**

- Tensor.Art: https://tensor.art/models/831702996753660780

---

### FurryGod E621 RNS

General furry concept LoRA for SDXL and Pony.

**Download:**

- Civitai: https://civitai.com/models/1010286/furrygod-e621-rns

---

## VRAM Requirements Summary

Your 2x RTX 5090 (64GB total) can handle everything easily:

| Model Type     | FP16/BF16 | FP8/Quantized | Notes                     |
| -------------- | --------- | ------------- | ------------------------- |
| SDXL/Pony      | 8-12GB    | 6-8GB         | Trivial for your hardware |
| SDXL + Refiner | 12-16GB   | 10-12GB       | Still very comfortable    |
| FLUX.1-dev     | 24-33GB   | 12-16GB       | Fits single 5090 at FP16  |
| FLUX.1-schnell | 20-28GB   | 10-14GB       | Faster inference          |

### GGUF Quantization (for FLUX)

If you want to run multiple FLUX instances:

| Quantization | VRAM    | Quality                   |
| ------------ | ------- | ------------------------- |
| Q8           | 12-14GB | Near-identical to FP16    |
| Q5           | 8-10GB  | Minimal quality loss      |
| Q4           | 6-8GB   | Moderate quality tradeoff |

**Source:** https://huggingface.co/city96/FLUX.1-dev-gguf

---

## Inference Backends

### ComfyUI (Recommended)

Best performance and VRAM efficiency for SDXL and FLUX.

| Property        | Value                               |
| --------------- | ----------------------------------- |
| Performance     | 2x faster than A1111 in batch tests |
| VRAM Efficiency | Best among all options              |
| Multi-GPU       | Supported via workflow nodes        |
| Video Support   | Yes (WAN, Hunyuan Video)            |

**RTX 5090 Setup:**

- Requires PyTorch 2.7+ with CUDA 12.8+
- Install via: https://github.com/comfyanonymous/ComfyUI

**GGUF Support:**

- Install: https://github.com/city96/ComfyUI-GGUF
- Models go in `ComfyUI/models/unet/`

---

### SwarmUI (Best for Multi-GPU)

Native multi-GPU support with distributed computing.

| Property  | Value                                      |
| --------- | ------------------------------------------ |
| Multi-GPU | First-class support ("Swarm" architecture) |
| Backend   | ComfyUI-based                              |
| License   | MIT                                        |

**Features:**

- Use multiple GPUs via color-coded workflows
- Network distributed rendering across machines
- Native SDXL and FLUX support

**Download:** https://github.com/mcmonkeyprojects/SwarmUI

**Multi-GPU Config:**

1. In Workflow Editor, set "MultiGPU selector" to "All"
2. Assign different colors to output nodes per GPU
3. First backend in list gets priority (put faster GPU first)

---

### Stable Diffusion WebUI Forge

A1111 interface with ComfyUI-level performance.

| Property    | Value                             |
| ----------- | --------------------------------- |
| Performance | 30-75% faster than standard A1111 |
| Interface   | Familiar A1111 layout             |
| VRAM        | ComfyUI-style memory management   |

**Best for:** Users who prefer traditional UI over node-based workflows.

---

## Recommended Setup for 2x RTX 5090

### Option A: Maximum Quality (Single GPU per Job)

Run ComfyUI with one model per GPU for parallel generation:

```
GPU 0: FLUX model (Chromafur Alpha) - 24-32GB
GPU 1: SDXL model (Pony/Midgard) - 8-12GB
```

### Option B: Multi-GPU Parallel (SwarmUI)

Use SwarmUI to distribute batch generations across both GPUs:

- Both GPUs run same model
- Batches split across GPUs
- 2x throughput for large generations

### Option C: Memory Pooling (NVLink if available)

If your 5090s support NVLink:

- Pool 64GB VRAM
- Run massive batch sizes
- Single model instance

---

## Quick Start Recommendations

### For Best Furry Art Quality:

1. **Model:** Midgard Pony V3.2 or Nova Furry XL
2. **Backend:** ComfyUI
3. **LoRA:** Furry Enhancer + PDV6XL Artist Tags (optional)

### For Maximum Flexibility:

1. **Model:** Pony Diffusion V6 XL (base)
2. **Backend:** SwarmUI
3. **LoRAs:** Mix character/artist LoRAs as needed

### For Best Prompt Following:

1. **Model:** Chromafur Alpha (FLUX-based)
2. **Backend:** ComfyUI with GGUF support
3. **Quantization:** FP16 or Q8 (you have the VRAM)

---

## Model Download Checklist

### Essential Models

| Model                | Size   | Link                                                                    |
| -------------------- | ------ | ----------------------------------------------------------------------- |
| Pony Diffusion V6 XL | ~6.5GB | [Civitai](https://civitai.com/models/257749/pony-diffusion-v6-xl)       |
| Midgard Pony V3.2    | ~6.5GB | [Civitai](https://civitai.com/models/470287/midgard-pony-thl-sdxl)      |
| Nova Furry XL        | ~6.5GB | [Civitai](https://civitai.com/models/503815/nova-furry-xl)              |
| Chromafur Alpha      | ~24GB  | [HuggingFace](https://huggingface.co/lodestone-horizon/chromafur-alpha) |

### Recommended LoRAs

| LoRA               | Size   | Link                                                       |
| ------------------ | ------ | ---------------------------------------------------------- |
| Furry Enhancer     | ~200MB | [Civitai](https://civitai.com/models/310964)               |
| PDV6XL Artist Tags | ~500MB | [Tensor.Art](https://tensor.art/models/705021682227773760) |

### Text Encoders (for FLUX)

| File                    | Link                                                                    |
| ----------------------- | ----------------------------------------------------------------------- |
| T5 XXL FP16             | [HuggingFace](https://huggingface.co/comfyanonymous/flux_text_encoders) |
| T5 XXL FP8 (lower VRAM) | Same repo, `t5xxl_fp8_e4m3fn`                                           |

---

## Downloaded Models (2026-01-24)

### Currently Downloaded

| Model                | Location                                                 | Size   | Notes          |
| -------------------- | -------------------------------------------------------- | ------ | -------------- |
| Pony Diffusion V6 XL | `/wyrmhdd/huggingface/LyliaEngine/Pony_Diffusion_V6_XL`  | ~6.5GB | Downloading... |
| Chromafur Alpha      | `/wyrmhdd/huggingface/lodestone-horizon/chromafur-alpha` | ~24GB  | Queued         |

### Quick Start Commands

```bash
# List downloaded models
ls -la /wyrmhdd/huggingface/models--*/

# Start ComfyUI (install first)
cd ~/ComfyUI && python main.py --listen 0.0.0.0 --port 8188

# For multi-GPU with SwarmUI
cd ~/SwarmUI && ./launch-linux.sh
```

---

## Example Prompts

### Pony Diffusion V6 XL

**Prompt Structure:**

```
score_9, score_8_up, score_7_up, source_furry, rating_explicit, [subject], [action], [setting], [style tags]
```

**Example - Anthro Wolf Portrait:**

```
score_9, score_8_up, score_7_up, source_furry, rating_safe,
solo, male, anthro wolf, blue eyes, grey fur, detailed fur texture,
portrait, looking at viewer, forest background,
digital painting, masterpiece, best quality
```

**Example - NSFW Scene:**

```
score_9, score_8_up, score_7_up, source_furry, rating_explicit,
solo, male, anthro dragon, muscular, detailed scales,
bedroom, lying on bed, seductive pose,
by kenket, by blotch, detailed background
```

**Negative Prompt (recommended):**

```
score_4, score_5, score_6, worst quality, low quality, blurry,
bad anatomy, bad hands, missing fingers, extra digits, deformed
```

**Settings:**

- Sampler: DPM++ 2M Karras or Euler A
- Steps: 25-40
- CFG: 6-8
- Clip Skip: 2 (important!)
- Resolution: 1024x1024 or 832x1216

### Chromafur Alpha (FLUX)

FLUX uses natural language prompts more effectively:

```
A highly detailed digital painting of an anthro wolf character with
grey fur and blue eyes, standing in a forest clearing at golden hour.
The character has a muscular build and is wearing casual modern clothing.
Hyper-detailed fur texture, cinematic lighting, professional artwork quality.
```

**Settings:**

- Steps: 20-30 (FLUX is faster)
- CFG: 3.5-4.5 (lower than SDXL)
- Resolution: 1024x1024

### Prompt Resources

| Resource        | URL                                             | Notes                     |
| --------------- | ----------------------------------------------- | ------------------------- |
| e621 Tags       | https://e621.net/tags                           | Official tag database     |
| Pony Tag Guide  | https://rentry.org/ponyxl_loras_n_stuff         | Community tag reference   |
| Civitai Prompts | https://civitai.com/images                      | Browse images for prompts |
| PromptHero      | https://prompthero.com/stable-diffusion-prompts | General SD prompts        |
| Danbooru Wiki   | https://danbooru.donmai.us/wiki_pages/help:tags | Anime tag reference       |

---

## RLHF / Fine-Tuning for Image Generation

### Open Source RLHF Tools

| Tool               | Purpose                                 | Link                                                                       |
| ------------------ | --------------------------------------- | -------------------------------------------------------------------------- |
| **diffusers-rlhf** | HuggingFace's RLHF for diffusion models | [GitHub](https://github.com/huggingface/diffusers/tree/main/examples/rlhf) |
| **DDPO**           | Denoising Diffusion Policy Optimization | [Paper](https://arxiv.org/abs/2305.13301)                                  |
| **DPO-Diffusion**  | Direct Preference Optimization for SD   | [GitHub](https://github.com/SalesforceAIResearch/DiffusionDPO)             |
| **AlignProp**      | Aligning Text-to-Image via Backprop     | [GitHub](https://github.com/Shentao-YANG/AlignProp_ICLR2024)               |
| **ImageReward**    | Human preference reward model           | [GitHub](https://github.com/THUDM/ImageReward)                             |

### Fine-Tuning Approaches

1. **LoRA Fine-Tuning** (easiest)
   - Train small adapter weights
   - Tools: kohya_ss, sd-scripts
   - Dataset: 20-100 images

2. **DreamBooth** (for specific subjects)
   - Fine-tune on specific character/style
   - Tools: kohya_ss, diffusers
   - Dataset: 5-20 images

3. **Full Fine-Tuning** (most expensive)
   - Retrain entire model
   - Tools: sd-scripts, diffusers
   - Dataset: 1000+ images

### Recommended RLHF Stack

```bash
# Install kohya_ss (most popular for LoRA training)
git clone https://github.com/bmaltais/kohya_ss.git
cd kohya_ss
./setup.sh

# Or use the GUI
python kohya_gui.py --listen 0.0.0.0 --port 7860
```

### ImageReward for Preference Learning

```python
# Install
pip install image-reward

# Use as reward model
import ImageReward as RM
model = RM.load("ImageReward-v1.0")
score = model.score(prompt, image_path)
```

---

## References

- [Pony Diffusion Guide](https://stable-diffusion-art.com/pony-diffusion-v6-xl/)
- [ComfyUI GGUF Low VRAM Guide](https://www.nextdiffusion.ai/tutorials/how-to-run-flux-dev-gguf-in-comfyui-low-vram-guide)
- [ComfyUI vs A1111 vs Forge 2025](https://apatero.com/blog/comfyui-vs-automatic1111-comparison-2025)
- [SwarmUI Multi-GPU Tutorial](https://github.com/FurkanGozukara/Stable-Diffusion/wiki/Master-Local-AI-Art-and-Video-Generation-with-SwarmUI-ComfyUI-Backend-The-Ultimate-2025-Tutorial)
- [RTX 5090 ComfyUI Setup](https://www.promptus.ai/blog/rtx-5090-comfyui-setup-guide)
- [Dual RTX 5090 Testing](https://huggingface.co/blog/HOSTKEY/more-5090-more-problems-testing-a-dual-nvidia-gpu)
