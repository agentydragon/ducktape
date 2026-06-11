# Local LLM inference on Intel Arc GPU via IPEX-LLM
#
# Runs Intel's IPEX-LLM fork of ollama in a Docker container with SYCL
# acceleration. Provides an OpenAI-compatible API at localhost:11434.
#
# Hardware: Intel Arc 130V/140V iGPU (Lunar Lake), needs /dev/dri passthrough.
# Image bundles oneAPI + Intel Compute Runtime + SYCL, avoiding NixOS packaging
# gaps (no DPC++/SYCL compiler in nixpkgs, nixpkgs#367722).
#
# Usage after enable:
#   podman exec ipex-ollama ollama pull qwen3:4b
#   curl http://localhost:11434/api/generate -d '{"model":"qwen3:4b","prompt":"Hello"}'
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.localLlm.arc;
in
{
  options.ducktape.localLlm.arc = {
    enable = lib.mkEnableOption "Local LLM inference on Intel Arc GPU (IPEX-LLM/ollama)";
  };

  config = lib.mkIf cfg.enable {
    # GPU compute drivers needed by the container
    hardware.graphics.extraPackages = with pkgs; [
      intel-compute-runtime
      intel-media-driver
      level-zero
    ];

    # Model storage: /var/lib/local-llm/ollama (shared parent with NPU models)
    systemd.tmpfiles.rules = [
      "d /var/lib/local-llm 0755 root root -"
      "d /var/lib/local-llm/ollama 0755 root root -"
    ];

    virtualisation.oci-containers.containers.ipex-ollama = {
      image = "intelanalytics/ipex-llm-inference-cpp-xpu:latest";
      extraOptions = [
        "--device=/dev/dri"
        "--shm-size=16g"
      ];
      ports = [ "127.0.0.1:11434:11434" ];
      environment = {
        OLLAMA_NUM_GPU = "999";
        ZES_ENABLE_SYSMAN = "1";
        DEVICE = "Arc";
        OLLAMA_HOST = "0.0.0.0";
        no_proxy = "localhost,127.0.0.1";
        SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS = "1";
        ONEAPI_DEVICE_SELECTOR = "level_zero:gpu";
        SYCL_DEVICE_FILTER = "gpu";
      };
      volumes = [
        "/var/lib/local-llm/ollama:/root/.ollama"
      ];
      entrypoint = "/bin/bash";
      cmd = [
        "-c"
        # ipex-llm-init sets oneAPI env; init-ollama symlinks the IPEX-LLM ollama binary + libs to /llm/ollama/;
        # LD_LIBRARY_PATH must include /llm/ollama so libggml-sycl.so and libggml-base.so are found
        "cd /llm/scripts && source ipex-llm-init --gpu --device Arc 2>/dev/null; mkdir -p /llm/ollama && cd /llm/ollama && init-ollama && export LD_LIBRARY_PATH=/llm/ollama:$LD_LIBRARY_PATH && exec ./ollama serve"
      ];
    };
  };
}
