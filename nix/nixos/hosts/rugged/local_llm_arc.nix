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
        # Gemma 4 E2B advertises 131k context, and a direct num_ctx=131072
        # request successfully loads on rugged. Set the service default so
        # OpenAI-compatible clients such as OpenCode get the same context.
        OLLAMA_CONTEXT_LENGTH = "131072";
      };
    };
  };
}
