# Codex configuration module
{
  pkgs,
  pkgsUnstable,
  lib,
  config,
  ...
}:
let
  codexSettings = {
    model = "gpt-5.1-codex";

    # Local model providers for GPT-OSS
    model_providers = {
      ollama = {
        name = "Ollama Local";
        base_url = "http://localhost:11434/v1";
        wire_api = "responses";
      };
      vllm = {
        name = "vLLM Local";
        base_url = "http://localhost:8000/v1";
        # vLLM's Responses API has incorrect GPT-OSS handling:
        # https://github.com/vllm-project/vllm/issues/28262
        # Returns reasoning_text in format Codex can't parse.
        wire_api = "chat";
      };
    };

    features = {
      streamable_shell = true;
      rmcp_client = true;
      unified_exec = true;
      view_image_tool = true;
      shell_tool = true; # enable `/shell`
      apply_patch_freeform = true; # freeform patch syntax
      # shell_snapshot ?  (keep off unless you want offline TUI snapshots)
    };
    # Multiple profiles let you switch between open‑source and OpenAI models.
    # The UI can pick a profile via the `/select_profile` command or by setting
    # `--config profile=NAME` when launching Codex.
    profiles = {
      openai = {
        model = "gpt-5.1-codex";
        # Web search requires OpenAI backend.
        features = {
          web_search_request = true;
        };
      };
      # GPT-OSS-20B via vLLM with Responses API
      gpt-oss = {
        model = "gpt-oss-20b";
        model_provider = "vllm";
        model_reasoning_effort = "high";
        features = {
          web_search_request = false;
        };
      };
      # GPT-OSS-20B via Ollama
      gpt-oss-ollama = {
        model = "gpt-oss:20b";
        model_provider = "ollama";
        features = {
          web_search_request = false;
        };
      };
    };
    # Persist command history to disk.
    # "save-all" will write every turn to ~/.codex/history.jsonl
    history = {
      persistence = "save-all";
    };
    shell_environment_policy = {
      "inherit" = "all";
      "set" = {
        CODEX_AGENT = "1";
      };
    };
    sandbox_mode = "workspace-write";
    sandbox_workspace_write = {
      writable_roots = [
        "/home/agentydragon/.cache/sccache"
        "/home/agentydragon/.cache/nix"
        "/nix"
        "/nix/var/nix"
        "/home/agentydragon/.cache/pre-commit"
        # Allow Codex sandboxed pre-commit runs to write their hook log.
        "/home/agentydragon/.cache/pre-commit/pre-commit.log"
      ];
      network_access = true;
      exclude_tmpdir_env_var = false;
      exclude_slash_tmp = false;
    };
  };

  tomlFormat = pkgs.formats.toml { };
  baseConfigFile = tomlFormat.generate "codex-config.nix-base" codexSettings;

  useXdgDirectories = config.home.preferXdgDirectories;
  xdgConfigHomeRelative = lib.removePrefix "${config.home.homeDirectory}/" config.xdg.configHome;
  codexHomeRelative = if useXdgDirectories then "${xdgConfigHomeRelative}/codex" else ".codex";
  codexHomeAbsolute =
    if useXdgDirectories then
      "${config.xdg.configHome}/codex"
    else
      "${config.home.homeDirectory}/.codex";

  baseFileRelative = "${codexHomeRelative}/config.nix-base.toml";
  baseFileAbsolute = "${codexHomeAbsolute}/config.nix-base.toml";
  liveFileAbsolute = "${codexHomeAbsolute}/config.toml";

  pythonMerge = pkgs.python3.withPackages (ps: [ ps."tomli-w" ]);

  mergeScript = ''
    set -euo pipefail

    CODEX_HOME='${codexHomeAbsolute}'
    BASE='${baseFileAbsolute}'
    LIVE='${liveFileAbsolute}'

    if [ ! -f "$BASE" ]; then
      exit 0
    fi

    mkdir -p "$CODEX_HOME"

    BASE="$BASE" LIVE="$LIVE" ${pythonMerge}/bin/python ${./merge.py}
  '';
in
{
  programs.codex = {
    enable = true;
    # Prefer the unstable codex package if available.
    package = pkgsUnstable.codex;
    # Avoid letting the upstream module overwrite ~/.codex/config.toml.
    # The activation script below handles merging our desired settings.
  };

  home = {
    file."${baseFileRelative}".source = baseConfigFile;
    activation.codexConfig = lib.hm.dag.entryAfter [ "writeBoundary" ] mergeScript;
    sessionVariables = lib.mkIf useXdgDirectories {
      CODEX_HOME = "${config.xdg.configHome}/codex";
    };
  };
}
