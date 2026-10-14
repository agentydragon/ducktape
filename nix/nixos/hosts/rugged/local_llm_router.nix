# Experimental single-model llama.cpp router on rugged's Intel Arc iGPU.
# Keep disabled until the separate router build passes sequential load/eviction
# and tool-call checks; production Ollama remains the default backend.
# Model presets preserve the six existing OpenCode/LiteLLM client IDs as explicit
# aliases. Qwen3.5's 262,144-token profile is experimental pending long-context
# verification. No draft model is materialized, so standalone llama-server uses
# its `spec-type=none` default rather than Ollama's MTP draft path.
# Before switching backends, use the declared Ollama prefetch configuration to
# cache all six unique base tags. The router reads that cache without changing it.
{
  config,
  lib,
  pkgs,
  llama-router,
  ...
}:
let
  cfg = config.ducktape.localLlm.llamaRouter;
  ollamaHomeDir = "/var/lib/local-llm/ollama-upstream";
  ollamaModelsDir = "${ollamaHomeDir}/models";
  routerStateDir = "/var/lib/rugged-llama-router";
  routerModelsDir = "${routerStateDir}/models";
  port = 11436;

  modelPreparation = pkgs.writeShellApplication {
    name = "prepare-rugged-llama-router-models";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.jq
    ];
    text = builtins.readFile ./prepare_llama_router_models.sh;
  };

  modelPresets = [
    {
      section = "rugged-qwen3-4b-instruct-2507-q4_K_M";
      alias = "qwen3:4b-instruct-2507-q4_K_M";
      source = "rugged-qwen3-4b-instruct-2507-q4_K_M";
      hasProjector = false;
    }
    {
      section = "rugged-gemma4-e2b-it-qat";
      alias = "gemma4:e2b-it-qat";
      source = "rugged-gemma4-e2b-it-qat";
      hasProjector = true;
    }
    {
      section = "rugged-gemma4-e4b";
      alias = "gemma4:e4b";
      source = "rugged-gemma4-e4b";
      hasProjector = true;
    }
    {
      section = "rugged-qwen2.5-coder-7b";
      alias = "qwen2.5-coder:7b";
      source = "rugged-qwen2.5-coder-7b";
      hasProjector = false;
    }
    {
      section = "rugged-qwen3-8b";
      alias = "qwen3:8b";
      source = "rugged-qwen3-8b";
      hasProjector = false;
    }
  ];
  baseModelPresets = lib.concatMapStringsSep "\n" (profile: ''
    [${profile.section}]
    alias = ${profile.alias}
    model = ${routerModelsDir}/${profile.source}/model.gguf
    ${lib.optionalString profile.hasProjector "mmproj = ${routerModelsDir}/${profile.source}/mmproj.gguf"}
  '') modelPresets;

  modelPreset = pkgs.writeText "rugged-llama-router-models.ini" ''
    version = 1

    [*]
    load-on-startup = false
    ctx-size = 32768
    batch-size = 512
    ubatch-size = 512
    n-gpu-layers = 999
    cache-type-k = f16
    cache-type-v = f16
    flash-attn = off
    parallel = 1
    jinja = true
    reasoning = off
    spec-type = none

    ${baseModelPresets}

    [rugged-gemma4-e2b-it-qat-long-128k-b128]
    alias = gemma4:e2b-it-qat-long-128k-b128
    model = ${routerModelsDir}/rugged-gemma4-e2b-it-qat-long-128k-b128/model.gguf
    mmproj = ${routerModelsDir}/rugged-gemma4-e2b-it-qat-long-128k-b128/mmproj.gguf
    ctx-size = 131072
    batch-size = 128
    ubatch-size = 128

    [rugged-qwen35-0.8b-256k]
    alias = qwen3.5:0.8b-long-256k-b256
    model = ${routerModelsDir}/rugged-qwen35-0.8b-256k/model.gguf
    mmproj = ${routerModelsDir}/rugged-qwen35-0.8b-256k/mmproj.gguf
    ctx-size = 262144
    batch-size = 256
    ubatch-size = 256
    cache-type-k = q4_0
    cache-type-v = q4_0
    flash-attn = on
  '';
in
{
  options.ducktape.localLlm.llamaRouter.enable =
    lib.mkEnableOption "experimental standalone llama.cpp router on rugged's Arc iGPU";

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !config.ducktape.localLlm.ollamaUpstream.enable;
        message = "ducktape.localLlm.llamaRouter and ducktape.localLlm.ollamaUpstream are mutually exclusive because both serve port 11436";
      }
      {
        assertion = !config.services.ollama.enable;
        message = "ducktape.localLlm.llamaRouter cannot run while services.ollama is enabled";
      }
    ];

    # Ollama creates its model blobs with a restrictive UMask, so the router
    # must use the same persistent owner instead of relying on group reads.
    # The backend assertion above guarantees these services never run together.
    users.groups.ollama = { };
    users.users.ollama = {
      isSystemUser = true;
      group = "ollama";
      home = ollamaHomeDir;
      extraGroups = [ "render" ];
    };

    environment.etc."llama-router/models.ini" = {
      source = modelPreset;
      mode = "0444";
    };

    systemd.services.rugged-llama-router = {
      description = "Experimental one-model-at-a-time llama.cpp router for rugged's Arc iGPU";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" ];
      unitConfig.RequiresMountsFor = ollamaModelsDir;
      serviceConfig = {
        Type = "simple";
        User = "ollama";
        Group = "ollama";
        SupplementaryGroups = [ "render" ];
        StateDirectory = "rugged-llama-router";
        StateDirectoryMode = "0750";
        UMask = "0077";
        ExecStartPre = lib.escapeShellArgs [
          (lib.getExe modelPreparation)
          ollamaModelsDir
          routerStateDir
        ];
        ExecStart = lib.escapeShellArgs [
          "${llama-router}/bin/llama-server"
          "--host"
          "127.0.0.1"
          "--port"
          (toString port)
          "--models-preset"
          "/etc/llama-router/models.ini"
          "--models-max"
          "1"
          "--offline"
        ];
        Restart = "on-failure";
        RestartSec = "5s";
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        ReadOnlyPaths = [ ollamaModelsDir ];
      };
    };
  };
}
