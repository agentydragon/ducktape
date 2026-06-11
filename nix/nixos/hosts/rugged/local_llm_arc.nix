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
  inputs,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.localLlm.arc;
  upstreamCfg = config.ducktape.localLlm.ollamaUpstream;
  pkgsMaster = import inputs.nixpkgs-master {
    inherit (pkgs.stdenv.hostPlatform) system;
    config.allowUnfree = true;
  };
in
{
  options.ducktape.localLlm.arc = {
    enable = lib.mkEnableOption "Local LLM inference on Intel Arc GPU (IPEX-LLM/ollama)";
  };

  options.ducktape.localLlm.ollamaUpstream = {
    enable = lib.mkEnableOption "newer upstream Ollama from the shared nixpkgs master pin";
  };

  config = lib.mkMerge [
    (lib.mkIf cfg.enable {
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
        # Resolved from docker.io/intelanalytics/ipex-llm-inference-cpp-xpu:latest
        # on 2026-06-05. Pin by digest so a rebuild does not silently change the
        # IPEX-patched Ollama stack.
        image = "intelanalytics/ipex-llm-inference-cpp-xpu@sha256:74c7fba6e12a083ff664ae54e1ff16a977a39caa03d272125db406eeddaee09e";
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
    })

    (lib.mkIf upstreamCfg.enable {
      systemd.tmpfiles.rules = [
        "d /var/lib/local-llm/ollama-upstream 0750 ollama ollama -"
        "d /var/lib/local-llm/ollama-upstream/models 0750 ollama ollama -"
      ];

      services.ollama = {
        enable = true;
        package = pkgsMaster.ollama-vulkan;
        user = "ollama";
        group = "ollama";
        host = "127.0.0.1";
        port = 11436;
        home = "/var/lib/local-llm/ollama-upstream";
        models = "/var/lib/local-llm/ollama-upstream/models";
        environmentVariables = {
          # Upstream Ollama's Vulkan backend detects the Lunar Lake iGPU but
          # drops integrated GPUs unless this is set.
          OLLAMA_IGPU_ENABLE = "1";
          # Gemma 4 E2B advertises 131k context, and a direct num_ctx=131072
          # request successfully loads on rugged. Set the service default so
          # OpenAI-compatible clients such as OpenCode get the same context.
          OLLAMA_CONTEXT_LENGTH = "131072";
        };
      };
    })
  ];
}
