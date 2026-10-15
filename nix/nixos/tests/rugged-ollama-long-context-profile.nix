# Test the Rugged-only experimental Ollama long-context profile.
#
# Run: nix eval --impure --file nix/nixos/tests/rugged-ollama-long-context-profile.nix

let
  flake = builtins.getFlake "path:${toString ../../..}";
  ruggedSystem = flake.nixosConfigurations.rugged;
  nixosLib = ruggedSystem.pkgs.lib;
  rugged =
    (ruggedSystem.extendModules {
      modules = [
        {
          ducktape.localLlm.ollamaUpstream.enable = nixosLib.mkForce true;
          ducktape.localLlm.llamaRouter.enable = nixosLib.mkForce false;
        }
      ];
    }).config;
  service = rugged.systemd.services.ollama-model-prefetch;
  alias = "gemma4:e2b-it-qat-long-128k-b128";
  modelfile = rugged.environment.etc."ollama/modelfiles/gemma4-e2b-it-qat-long-128k-b128".source;
  ruggedWithRouter =
    (ruggedSystem.extendModules {
      modules = [
        {
          ducktape.localLlm.ollamaUpstream.enable = nixosLib.mkForce false;
          ducktape.localLlm.llamaRouter.enable = nixosLib.mkForce true;
        }
      ];
    }).config;
  routerPreset = builtins.readFile ruggedWithRouter.environment.etc."llama-router/models.ini".source;
  pinnedManifest = builtins.fromJSON (builtins.readFile ../hosts/rugged/llama_router_artifacts.json);
  routerPresetLines = nixosLib.splitString "\n" routerPreset;
  findLineIndex =
    predicate: lines:
    let
      search =
        index: remaining:
        if remaining == [ ] then
          null
        else if predicate (builtins.head remaining) then
          index
        else
          search (index + 1) (builtins.tail remaining);
    in
    search 0 lines;
  isPresetSectionHeader =
    line:
    builtins.stringLength line >= 2
    && builtins.substring 0 1 line == "["
    && builtins.substring (builtins.stringLength line - 1) 1 line == "]";
  sectionLines =
    section:
    let
      start = findLineIndex (line: line == "[${section}]") routerPresetLines;
      takeUntilHeader =
        remaining:
        if remaining == [ ] || isPresetSectionHeader (builtins.head remaining) then
          [ ]
        else
          [ (builtins.head remaining) ] ++ takeUntilHeader (builtins.tail remaining);
    in
    if start == null then [ ] else takeUntilHeader (nixosLib.drop (start + 1) routerPresetLines);
  hasRouterPresetLineIn = section: line: builtins.elem line (sectionLines section);
  gemmaLongModelSha =
    pinnedManifest.artifacts.${pinnedManifest.profiles.rugged-gemma4-e2b-it-qat-long-128k-b128.model_artifact}.sha256;
  qwenLongModelSha =
    pinnedManifest.artifacts.${(builtins.getAttr "rugged-qwen35-0.8b-256k" pinnedManifest.profiles).model_artifact}.sha256;
  routerService = ruggedWithRouter.systemd.services.rugged-llama-router;
  provisionService = ruggedWithRouter.systemd.services.rugged-llama-model-provision;
in
{
  test_flash_attention_and_kv_cache_match_qualification = {
    expr = {
      inherit (rugged.systemd.services.ollama.environment) OLLAMA_FLASH_ATTENTION OLLAMA_KV_CACHE_TYPE;
    };
    expected = {
      OLLAMA_FLASH_ATTENTION = "0";
      OLLAMA_KV_CACHE_TYPE = "f16";
    };
  };

  test_server_default_remains_at_qualified_32k = {
    expr = rugged.systemd.services.ollama.environment.OLLAMA_CONTEXT_LENGTH;
    expected = "32768";
  };

  test_alias_modelfile_bakes_context_and_batch = {
    expr = builtins.readFile modelfile;
    expected = ''
      FROM gemma4:e2b-it-qat
      PARAMETER num_ctx 131072
      PARAMETER num_batch 128
    '';
  };

  test_prefetch_creates_alias_after_declared_base_fetch = {
    expr = {
      base_in_script = builtins.match ".*gemma4:e2b-it-qat.*" service.script != null;
      alias_in_script = builtins.match ".*ollama create ${alias} -f.*" service.script != null;
      modelfile_store_path_in_script =
        builtins.replaceStrings [ (toString modelfile) ] [ "" ] service.script != service.script;
      restart_if_changed = service.restartIfChanged;
      remain_after_exit = service.serviceConfig.RemainAfterExit;
    };
    expected = {
      base_in_script = true;
      alias_in_script = true;
      modelfile_store_path_in_script = true;
      restart_if_changed = true;
      remain_after_exit = true;
    };
  };

  test_router_switch_keeps_only_one_backend_on_the_shared_port = {
    expr = {
      router_enabled = ruggedWithRouter.ducktape.localLlm.llamaRouter.enable;
      ollama_disabled = !ruggedWithRouter.services.ollama.enable;
      router_service_installed = builtins.elem "multi-user.target" routerService.wantedBy;
      router_requires_cached_model_mount =
        routerService.unitConfig.RequiresMountsFor == "/var/lib/rugged-llm/models";
      router_requires_independent_provisioner =
        builtins.elem "rugged-llama-model-provision.service" routerService.requires
        && builtins.elem "rugged-llama-model-provision.service" routerService.after;
      router_reuses_the_persistent_cache_owner =
        routerService.serviceConfig.User == "ollama"
        && routerService.serviceConfig.Group == "ollama"
        && !(routerService.serviceConfig.DynamicUser or false)
        && routerService.serviceConfig.UMask == "0077"
        && ruggedWithRouter.users.users.ollama.isSystemUser
        && ruggedWithRouter.users.users.ollama.group == "ollama"
        && ruggedWithRouter.users.users.ollama.uid == rugged.users.users.ollama.uid
        && ruggedWithRouter.users.groups.ollama.gid == rugged.users.groups.ollama.gid
        && ruggedWithRouter.users.users.ollama.home == "/var/lib/local-llm/ollama-upstream"
        && builtins.elem "render" ruggedWithRouter.users.users.ollama.extraGroups;
      router_keeps_model_cache_read_only = builtins.elem "/var/lib/rugged-llm/models" routerService.serviceConfig.ReadOnlyPaths;
      router_binds_shared_loopback_port_and_limits_residency =
        builtins.match ".*--host 127\\.0\\.0\\.1 --port 11436.*--models-max 1.*" routerService.serviceConfig.ExecStart
        != null;
    };
    expected = {
      router_enabled = true;
      ollama_disabled = true;
      router_service_installed = true;
      router_requires_cached_model_mount = true;
      router_requires_independent_provisioner = true;
      router_reuses_the_persistent_cache_owner = true;
      router_keeps_model_cache_read_only = true;
      router_binds_shared_loopback_port_and_limits_residency = true;
    };
  };

  test_router_provisioner_is_cache_first_and_independent_of_ollama = {
    expr = {
      type = provisionService.serviceConfig.Type;
      remain_after_exit = provisionService.serviceConfig.RemainAfterExit;
      user = provisionService.serviceConfig.User;
      group = provisionService.serviceConfig.Group;
      state_directory = provisionService.serviceConfig.StateDirectory;
      timeout = provisionService.serviceConfig.TimeoutStartSec;
      umask = provisionService.serviceConfig.UMask;
      explicit_tls_ca_bundle = nixosLib.hasInfix "/etc/ssl/certs/ca-bundle.crt" provisionService.environment.SSL_CERT_FILE;
      exec_uses_pinned_manifest_and_optional_ollama_import =
        builtins.match ".*/etc/llama-router/artifacts\\.json /var/lib/rugged-llm/models /var/lib/local-llm/ollama-upstream/models/blobs.*" provisionService.serviceConfig.ExecStart
        != null;
      has_no_network_online_dependency =
        !(builtins.elem "network-online.target" (provisionService.wants or [ ]))
        && !(builtins.elem "network-online.target" (provisionService.after or [ ]));
      router_restarts_when_manifest_or_preset_changes =
        builtins.length provisionService.restartTriggers == 2
        && builtins.length routerService.restartTriggers == 2
        && nixosLib.hasInfix "rugged-llama-router-artifacts.json" (
          toString (builtins.elemAt provisionService.restartTriggers 0)
        )
        && builtins.elem (builtins.elemAt provisionService.restartTriggers 0) routerService.restartTriggers
        && nixosLib.hasInfix "rugged-llama-router-models.ini" (
          toString (builtins.elemAt routerService.restartTriggers 1)
        );
      helper_change_restarts_provisioner = nixosLib.hasInfix "provision-rugged-llama-router-models" (
        toString (builtins.elemAt provisionService.restartTriggers 1)
      );
    };
    expected = {
      type = "oneshot";
      remain_after_exit = true;
      user = "ollama";
      group = "ollama";
      state_directory = "rugged-llm";
      timeout = "8h";
      umask = "0077";
      explicit_tls_ca_bundle = true;
      exec_uses_pinned_manifest_and_optional_ollama_import = true;
      has_no_network_online_dependency = true;
      router_restarts_when_manifest_or_preset_changes = true;
      helper_change_restarts_provisioner = true;
    };
  };

  test_router_artifact_manifest_is_fully_pinned = {
    expr = {
      artifact_count = builtins.length (builtins.attrNames pinnedManifest.artifacts);
      profile_count = builtins.length (builtins.attrNames pinnedManifest.profiles);
      digests_are_sha256 = builtins.all (artifact: builtins.stringLength artifact.sha256 == 64) (
        builtins.attrValues pinnedManifest.artifacts
      );
      urls_are_digest_addressed = builtins.all (
        artifact: nixosLib.hasInfix ("sha256:" + artifact.sha256) artifact.url
      ) (builtins.attrValues pinnedManifest.artifacts);
      sizes_are_positive = builtins.all (artifact: artifact.size_bytes > 0) (
        builtins.attrValues pinnedManifest.artifacts
      );
      every_model_preset_points_at_a_pinned_blob = builtins.all (
        profile:
        nixosLib.hasInfix "model = /var/lib/rugged-llm/models/blobs/sha256-${pinnedManifest.artifacts.${profile.model_artifact}.sha256}" routerPreset
      ) (builtins.attrValues pinnedManifest.profiles);
      every_projector_preset_points_at_a_pinned_blob = builtins.all (
        profile:
        !(profile ? projector_artifact)
        || nixosLib.hasInfix "mmproj = /var/lib/rugged-llm/models/blobs/sha256-${pinnedManifest.artifacts.${profile.projector_artifact}.sha256}" routerPreset
      ) (builtins.attrValues pinnedManifest.profiles);
      long_gemma_shares_base_artifact =
        pinnedManifest.profiles.rugged-gemma4-e2b-it-qat-long-128k-b128.model_artifact
        == pinnedManifest.profiles.rugged-gemma4-e2b-it-qat.model_artifact;
    };
    expected = {
      artifact_count = 9;
      profile_count = 7;
      digests_are_sha256 = true;
      urls_are_digest_addressed = true;
      sizes_are_positive = true;
      every_model_preset_points_at_a_pinned_blob = true;
      every_projector_preset_points_at_a_pinned_blob = true;
      long_gemma_shares_base_artifact = true;
    };
  };

  test_router_preserves_the_qualified_gemma_profile = {
    expr =
      builtins.all (hasRouterPresetLineIn "*") [
        "cache-type-k = f16"
        "cache-type-v = f16"
        "flash-attn = off"
        "reasoning = off"
      ]
      && builtins.all (hasRouterPresetLineIn "rugged-gemma4-e2b-it-qat-long-128k-b128") [
        "alias = gemma4:e2b-it-qat-long-128k-b128"
        "model = /var/lib/rugged-llm/models/blobs/sha256-${gemmaLongModelSha}"
        "ctx-size = 131072"
        "batch-size = 128"
        "ubatch-size = 128"
      ];
    expected = true;
  };

  test_router_qwen35_long_profile_sets_its_own_memory_flags = {
    expr = builtins.all (hasRouterPresetLineIn "rugged-qwen35-0.8b-256k") [
      "alias = qwen3.5:0.8b-long-256k-b256"
      "model = /var/lib/rugged-llm/models/blobs/sha256-${qwenLongModelSha}"
      "ctx-size = 262144"
      "batch-size = 256"
      "ubatch-size = 256"
      "cache-type-k = q4_0"
      "cache-type-v = q4_0"
      "flash-attn = on"
    ];
    expected = true;
  };
}
