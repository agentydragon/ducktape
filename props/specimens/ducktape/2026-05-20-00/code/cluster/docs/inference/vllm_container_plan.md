# Plan: User-Level vLLM Container Service in Home-Manager

## Goal

Create a home-manager managed systemd user service for vLLM using the official container image with GPU passthrough.

## Context

- GPU passthrough chain: Metal → Proxmox VM → Container (works)
- Model storage: `/wyrmhdd/huggingface` (bind mount into container)
- **Model: AWQ 4-bit quantized** (`cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit`)
- Context: **131K tokens** (enabled by AWQ reducing weights from 28.5 GB to ~7.6 GB per GPU)
- OpenCode already configured for vLLM on port 8000
- Docker storage moved to `/wyrmhdd/docker` (root SSD too small for ~20GB image)

## Quick Start

```bash
# Start vLLM with AWQ model
~/code/ducktape/experimental/local-llm/start-vllm-awq.sh

# Or manually:
docker run --rm --name vllm \
  --gpus all \
  -v /wyrmhdd/huggingface:/root/.cache/huggingface \
  -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit \
  --quantization awq \
  --served-model-name qwen3-coder-awq \
  --tensor-parallel-size 2 \
  --max-model-len 131072 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder
```

## Implementation (Systemd Service)

### 1. Create vLLM service module

**File**: `nix/home/services/vllm.nix`

```nix
{ config, pkgs, lib, ... }:
{
  systemd.user.services.vllm = {
    Unit = {
      Description = "vLLM inference server (AWQ, tensor parallel)";
      After = [ "network-online.target" ];
      Wants = [ "network-online.target" ];
    };
    Service = {
      Type = "simple";
      ExecStart = "/usr/bin/docker run --rm --name vllm \
        --gpus all \
        -v /wyrmhdd/huggingface:/root/.cache/huggingface \
        -p 8000:8000 \
        vllm/vllm-openai:latest \
        --model cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit \
        --quantization awq \
        --served-model-name qwen3-coder-awq \
        --tensor-parallel-size 2 \
        --max-model-len 131072 \
        --gpu-memory-utilization 0.90 \
        --enable-auto-tool-choice \
        --tool-call-parser qwen3_coder";
      ExecStop = "/usr/bin/docker stop vllm";
      Restart = "on-failure";
      RestartSec = "10";
    };
    Install = {
      WantedBy = [ "default.target" ];
    };
  };
}
```

### 2. Import in wyrm.nix

**File**: `nix/home/hosts/wyrm.nix`

Add to imports:

```nix
imports = [
  ../home.nix
  ../opencode
  ../services/vllm.nix  # Add this
];
```

### 3. Prerequisites (one-time)

```bash
# Ensure NVIDIA Container Toolkit / CDI is configured
# Pull the vLLM image
docker pull vllm/vllm-openai:latest

# Pre-download the AWQ model (optional, vLLM will download on first run)
python -c "from huggingface_hub import snapshot_download; snapshot_download('cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit')"
```

## Verification

```bash
# Apply config
home-manager switch --flake .#wyrm --impure

# Check service status
systemctl --user status vllm

# Start service
systemctl --user start vllm

# Watch logs
journalctl --user -u vllm -f

# Test API
curl http://localhost:8000/v1/models

# Check GPU usage
nvidia-smi
```

## Notes

- Container handles all Python/CUDA deps - fully isolated
- Models cached on `/wyrmhdd/huggingface`, persist across restarts
- `--rm` ensures clean container on restart
- Disable auto-start: `systemctl --user disable vllm`

## Why AWQ?

The bf16 model (61 GB total) requires 28.5 GB per GPU with TP=2, leaving only 2.85 GiB for KV cache and activations on 32 GB GPUs. This causes OOM at any meaningful context length.

AWQ 4-bit quantization reduces weights to ~7.6 GB per GPU, leaving ~23 GB for KV cache = 131K+ context.

### Memory comparison

| Model         | Weights/GPU | KV Budget | Max Context  |
| ------------- | ----------- | --------- | ------------ |
| bf16          | 28.5 GB     | ~2.8 GB   | 32K (barely) |
| **AWQ 4-bit** | ~7.6 GB     | ~23 GB    | **131K+**    |

## VM GPU Passthrough Notes

- **No GPU P2P**: "Custom allreduce is disabled" - GPUs communicate through CPU/PCIe, not direct P2P
- **Impact**: Slightly higher latency, but no significant memory overhead
- **Not the cause of OOM**: The OOM was due to bf16 weight size, not P2P overhead
