# Intel Lunar Lake NPU driver setup for LLM inference
#
# Enables the NPU kernel driver and userspace stack, and runs a Nix-native
# llama-server (see nix/packages/{openvino-npu,llama-cpp-openvino}.nix) as a
# router serving both models over the NPU, behind a bearer-token nginx proxy
# on the port litellm's Service/EndpointSlice already targets
# (cluster/k8s/litellm/app/rugged-npu-llm-endpoints.yaml).
#
# Hardware: Intel Lunar Lake NPU (PCI 8086:643e), /dev/accel/accel0.
{
  config,
  lib,
  pkgs,
  username,
  llama-cpp-openvino,
  ...
}:
let
  cfg = config.ducktape.localLlm.npu;

  llamaServerPort = 8090; # loopback-only; the bearer proxy is the only public listener
  bearerProxyPort = 18080; # matches rugged-npu-llm-endpoints.yaml's Service/EndpointSlice

  # Model IDs the router exposes are exactly these filenames (minus .gguf) --
  # litellm's proxy-config.yaml sends "model": "qwen3-4b" / "llama-3.2-1b".
  # Quantization: Q4_0 for both -- the OpenVINO NPU backend's primary
  # supported scheme (docs/backend/OPENVINO.md). Qwen3-4B-Instruct-2507 is
  # Qwen's non-thinking instruct release; upstream's own NPU validation table
  # covers Qwen3-8B, not the 4B, so this pairing is unvalidated by upstream
  # (Llama-3.2-1B-Instruct is validated on NPU at Q4_0).
  models = pkgs.linkFarm "rugged-npu-llm-models" [
    {
      name = "qwen3-4b.gguf";
      path = pkgs.fetchurl {
        url = "https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_0.gguf";
        sha256 = "e0ba675d86ab277c61701c6793659b2ae801d95e3be791464c321e6fbf613be2";
      };
    }
    {
      name = "llama-3.2-1b.gguf";
      path = pkgs.fetchurl {
        url = "https://huggingface.co/unsloth/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_0.gguf";
        sha256 = "66bfbb2d48bdb77cd56bd03ef820deff3c4a74b1a09de3b917ae13e72c1a70c2";
      };
    }
  ];
in
{
  options.ducktape.localLlm.npu = {
    enable = lib.mkEnableOption "Intel NPU driver setup for LLM inference";
  };

  config = lib.mkIf cfg.enable {
    hardware.cpu.intel.npu.enable = true;

    # Grant user access to /dev/accel/*
    users.users.${username}.extraGroups = [ "render" ];

    users.users.rugged-npu-llm = {
      isSystemUser = true;
      group = "rugged-npu-llm";
      extraGroups = [ "render" ];
    };
    users.groups.rugged-npu-llm = { };

    systemd.services.rugged-npu-llm = {
      description = "llama-server (OpenVINO NPU backend) router for Qwen3-4B + Llama-3.2-1B";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      environment = {
        # Selects OpenVINO's NPU device; ggml-openvino defaults to CPU otherwise
        # (ggml/src/ggml-openvino/ggml-openvino-extra.cpp).
        GGML_OPENVINO_DEVICE = "NPU";
      };
      serviceConfig = {
        ExecStart = ''
          ${llama-cpp-openvino}/bin/llama-server \
            --host 127.0.0.1 --port ${toString llamaServerPort} \
            --models-dir ${models} \
            --flash-attn on \
            --sleep-idle-seconds 300
        '';
        User = "rugged-npu-llm";
        Group = "rugged-npu-llm";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    # Bearer-checking proxy, mirroring cluster/k8s/activitywatch/bearer-proxy.conf.template's
    # pattern: the token lives in the same SOPS file litellm reads
    # (cluster/k8s/litellm/secrets/rugged-npu-llm-bearer-token.sops.yaml), so
    # cluster and host authenticate with one shared value.
    sops.secrets.rugged_npu_llm_bearer_token = {
      sopsFile = ../../../../cluster/k8s/litellm/secrets/rugged-npu-llm-bearer-token.sops.yaml;
      key = "stringData.token";
    };

    sops.templates."rugged-npu-llm-nginx.conf".content = ''
      map $http_authorization $rugged_npu_llm_ok {
        default 0;
        "Bearer ${config.sops.placeholder.rugged_npu_llm_bearer_token}" 1;
      }

      server {
        listen ${toString bearerProxyPort};

        location = /healthz {
          return 200 "ok";
        }

        location / {
          if ($rugged_npu_llm_ok = 0) {
            return 401;
          }
          proxy_pass http://127.0.0.1:${toString llamaServerPort};
          proxy_set_header Host $host;
          proxy_buffering off; # llama-server streams SSE token-by-token
        }
      }
    '';

    services.nginx = {
      enable = true;
      appendHttpConfig = ''
        include ${config.sops.templates."rugged-npu-llm-nginx.conf".path};
      '';
    };

    networking.firewall.allowedTCPPorts = [ bearerProxyPort ];
  };
}
