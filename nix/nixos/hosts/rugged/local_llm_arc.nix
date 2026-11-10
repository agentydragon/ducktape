# Upstream Ollama (Vulkan) on rugged's Intel Arc 130V/140V iGPU (Lunar Lake).
#
# Do not restore the retired IPEX-LLM fork: its archived backend stopped at
# Ollama 0.9.3. Runtime experiments: nix/debug/rugged/hw/gemma4.md.
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
  modelfileName = tag: lib.replaceStrings [ ":" "/" ] [ "-" "-" ] tag;
  modelfiles = lib.mapAttrs (
    tag: alias:
    pkgs.writeText "ollama-modelfile-${modelfileName tag}" ''
      FROM ${alias.from}
      PARAMETER num_ctx ${toString alias.numCtx}
      PARAMETER num_batch ${toString alias.numBatch}
    ''
  ) upstreamCfg.aliases;
  hasModelPrefetchWork = upstreamCfg.models != [ ] || upstreamCfg.aliases != { };
in
{
  options.ducktape.localLlm.ollamaUpstream = {
    enable = lib.mkEnableOption "newer upstream Ollama from the shared nixpkgs master pin";
    models = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Base model tags to fetch on activation when they are not already cached.";
    };
    aliases = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            from = lib.mkOption {
              type = lib.types.str;
              description = "Cached base model tag used by the Modelfile.";
            };
            numCtx = lib.mkOption {
              type = lib.types.ints.positive;
              description = "Context allocation baked into the alias Modelfile.";
            };
            numBatch = lib.mkOption {
              type = lib.types.ints.positive;
              description = "Batch size baked into the alias Modelfile.";
            };
          };
        }
      );
      default = { };
      description = "Named Ollama aliases whose Modelfiles are refreshed after base pulls.";
    };
  };

  config = lib.mkIf upstreamCfg.enable {
    assertions = [
      {
        assertion = lib.all (alias: builtins.elem alias.from upstreamCfg.models) (
          lib.attrValues upstreamCfg.aliases
        );
        message = "Every local Ollama alias must declare its base tag in ducktape.localLlm.ollamaUpstream.models";
      }
    ];

    systemd.tmpfiles.rules = [
      "d /var/lib/local-llm/ollama-upstream 0750 ollama ollama -"
      "d /var/lib/local-llm/ollama-upstream/models 0750 ollama ollama -"
    ];

    environment.etc = lib.mapAttrs' (tag: modelfile: {
      name = "ollama/modelfiles/${modelfileName tag}";
      value = {
        source = modelfile;
        mode = "0444";
      };
    }) modelfiles;

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
        # The experimental long-context Gemma alias requires Flash Attention off.
        # This is an Ollama-wide setting, so it also changes the qualified 32k profiles.
        OLLAMA_FLASH_ATTENTION = "0";
        # Match the KV cache type used in the successful long-context qualification.
        OLLAMA_KV_CACHE_TYPE = "f16";
        # Keep the server default at the qualified 32k profile. The experimental
        # long-context alias bakes its larger allocation into its Modelfile.
        OLLAMA_CONTEXT_LENGTH = "32768";
        # Keep GPU memory use aligned with the single-request benchmark setup.
        OLLAMA_NUM_PARALLEL = "1";
        OLLAMA_MAX_LOADED_MODELS = "1";
      };
    };

    systemd.services.ollama-model-prefetch = lib.mkIf hasModelPrefetchWork {
      description = "Fetch local Ollama models and refresh declared aliases";
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
        # Keep the unit active after prefetch so a changed unit definition
        # (including a changed declared model list) is restarted on nixos-rebuild.
        RemainAfterExit = true;
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
        ${lib.concatStringsSep "\n" (
          lib.mapAttrsToList (tag: modelfile: ''
            ${lib.getExe config.services.ollama.package} create ${
              lib.escapeShellArgs [
                tag
                "-f"
                modelfile
              ]
            }
          '') modelfiles
        )}
      '';
    };
  };
}
