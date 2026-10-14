# Test the Rugged-only experimental Ollama long-context profile.
#
# Run: nix eval --impure --file nix/nixos/tests/rugged-ollama-long-context-profile.nix

let
  flake = builtins.getFlake "path:${toString ../../..}";
  rugged = flake.nixosConfigurations.rugged.config;
  service = rugged.systemd.services.ollama-model-prefetch;
  alias = "gemma4:e2b-it-qat-long-128k-b128";
  modelfile = rugged.environment.etc."ollama/modelfiles/gemma4-e2b-it-qat-long-128k-b128".source;
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
}
