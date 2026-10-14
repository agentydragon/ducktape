# Upstream Ollama (Vulkan) on rugged's Intel Arc 130V/140V iGPU (Lunar Lake).
#
# RETIRED (2026-09-17): this file used to also run IPEX-LLM's SYCL-accelerated
# Ollama fork (`ducktape.localLlm.arc`). Intel archived intel/ipex-llm
# 2026-01-28 ("known security issues", no more patches); its bundled Ollama
# was frozen at 0.9.3 forever, too old to ever run Gemma 4. See
# nix/debug/rugged/hw/llm_arc_gpu.md for detail.
{
  config,
  inputs,
  lib,
  pkgs,
  ...
}:
let
  upstreamCfg = config.ducktape.localLlm.ollamaUpstream;
  pkgsMaster = import inputs.nixpkgs-master {
    inherit (pkgs.stdenv.hostPlatform) system;
    config.allowUnfree = true;
  };
in
{
  options.ducktape.localLlm.ollamaUpstream = {
    enable = lib.mkEnableOption "newer upstream Ollama from the shared nixpkgs master pin";
    models = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Local models to fetch on activation when they are not already cached.";
    };
  };

  config = lib.mkIf upstreamCfg.enable {
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
        # This host serves local models only; disable Ollama cloud features.
        OLLAMA_NO_CLOUD = "1";
        # Match the context limit measured for the current rugged serving
        # harness. Larger contexts have not been qualified on this service.
        OLLAMA_CONTEXT_LENGTH = "32768";
      };
    };

    systemd.services.ollama-model-prefetch = lib.mkIf (upstreamCfg.models != [ ]) {
      description = "Fetch missing declared local Ollama models";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [
        "network-online.target"
        "ollama.service"
      ];
      bindsTo = [ "ollama.service" ];
      environment = config.systemd.services.ollama.environment;
      serviceConfig = {
        Type = "oneshot";
        User = "ollama";
        Group = "ollama";
        UMask = "0077";
      };
      script = ''
        for model in ${lib.escapeShellArgs upstreamCfg.models}; do
          if ! model_list=$(${lib.getExe config.services.ollama.package} list); then
            echo "failed to inspect the local Ollama model cache" >&2
            exit 1
          fi
          if printf '%s\n' "$model_list" | ${lib.getExe pkgs.gawk} -v model="$model" 'NR > 1 && $1 == model { found=1 } END { exit !found }'; then
            echo "model $model is already cached; skipping pull"
          else
            ${lib.getExe config.services.ollama.package} pull "$model"
          fi
        done
      '';
    };
  };
}
