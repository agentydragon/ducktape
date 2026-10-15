# Experimental single-model llama.cpp router on rugged's Intel Arc iGPU.
# Five base profiles preserve the existing 32k OpenCode/LiteLLM client IDs;
# longer-context aliases below remain experimental pending further qualification.
# Qwen3.5's 262,144-token profile is experimental pending long-context verification.
# No draft model is materialized, so standalone llama-server uses its
# `spec-type=none` default rather than Ollama's MTP draft path.
# Model artifacts are pinned by digest in llama_router_artifacts.json and are
# provisioned into /var/lib/rugged-llm/models before the router starts.
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
  ollamaBlobsDir = "${ollamaModelsDir}/blobs";
  modelStoreStateDir = "/var/lib/rugged-llm";
  modelStoreDir = "${modelStoreStateDir}/models";
  modelBlobsDir = "${modelStoreDir}/blobs";
  port = 11436;

  artifactManifest = builtins.fromJSON (builtins.readFile ./llama_router_artifacts.json);
  artifactManifestFile = pkgs.writeText "rugged-llama-router-artifacts.json" (
    builtins.readFile ./llama_router_artifacts.json
  );
  artifactBlobPath =
    artifactId: "${modelBlobsDir}/sha256-${artifactManifest.artifacts.${artifactId}.sha256}";

  modelProvisioner = pkgs.writeShellApplication {
    name = "provision-rugged-llama-router-models";
    runtimeInputs = [
      llama-router
      pkgs.coreutils
      pkgs.jq
    ];
    text = builtins.readFile ./provision_llama_router_models.sh;
  };

  modelPresets = lib.mapAttrsToList (section: profile: {
    inherit section;
    inherit (profile) alias;
    modelArtifact = profile.model_artifact;
    projectorArtifact = profile.projector_artifact or null;
  }) artifactManifest.profiles;
  profilePreset = profile: ''
    [${profile.section}]
    alias = ${profile.alias}
    model = ${artifactBlobPath profile.modelArtifact}
    ${lib.optionalString (
      profile.projectorArtifact != null
    ) "mmproj = ${artifactBlobPath profile.projectorArtifact}"}
  '';
  baseModelPresets = lib.concatMapStringsSep "\n" profilePreset (
    builtins.filter (
      profile:
      profile.section != "rugged-gemma4-e2b-it-qat-long-128k-b128"
      && profile.section != "rugged-qwen35-0.8b-256k"
    ) modelPresets
  );
  gemmaLongProfile = builtins.head (
    builtins.filter (profile: profile.section == "rugged-gemma4-e2b-it-qat-long-128k-b128") modelPresets
  );
  qwenLongProfile = builtins.head (
    builtins.filter (profile: profile.section == "rugged-qwen35-0.8b-256k") modelPresets
  );

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

    ${profilePreset gemmaLongProfile}
    ctx-size = 131072
    batch-size = 128
    ubatch-size = 128

    ${profilePreset qwenLongProfile}
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

    environment.etc = {
      "llama-router/models.ini" = {
        source = modelPreset;
        mode = "0444";
      };
      "llama-router/artifacts.json" = {
        source = artifactManifestFile;
        mode = "0444";
      };
    };

    systemd.services.rugged-llama-model-provision = {
      description = "Verify and provision pinned GGUF artifacts for rugged's llama.cpp router";
      restartTriggers = [
        artifactManifestFile
        modelProvisioner
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "ollama";
        Group = "ollama";
        StateDirectory = "rugged-llm";
        StateDirectoryMode = "0750";
        UMask = "0077";
        ExecStart = lib.escapeShellArgs [
          (lib.getExe modelProvisioner)
          "/etc/llama-router/artifacts.json"
          modelStoreDir
          ollamaBlobsDir
        ];
        TimeoutStartSec = "8h";
        TimeoutStopSec = "30s";
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
      };
      environment.SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    };

    services.llama-cpp = {
      enable = true;
      package = llama-router;
      host = "127.0.0.1";
      inherit port;
      extraFlags = [
        "--models-preset"
        "/etc/llama-router/models.ini"
        "--models-max"
        "1"
        "--offline"
      ];
    };

    systemd.services.llama-cpp = {
      requires = [ "rugged-llama-model-provision.service" ];
      after = [
        "local-fs.target"
        "rugged-llama-model-provision.service"
      ];
      restartTriggers = [
        artifactManifestFile
        modelPreset
      ];
      unitConfig.RequiresMountsFor = modelStoreDir;
      serviceConfig = {
        User = "ollama";
        Group = "ollama";
        SupplementaryGroups = [ "render" ];
        # Keep the provisioned model store and the module's working directory.
        StateDirectory = lib.mkForce "rugged-llm llama-cpp";
        StateDirectoryMode = "0750";
        UMask = "0077";
        DynamicUser = lib.mkForce false;
        # Preserve the render group in the host namespace for Arc access.
        PrivateUsers = lib.mkForce false;
        # The Vulkan driver generates executable code at runtime.
        MemoryDenyWriteExecute = lib.mkForce false;
        NoNewPrivileges = true;
        ReadOnlyPaths = [ modelStoreDir ];
      };
    };
  };
}
