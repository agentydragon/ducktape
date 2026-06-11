# Codex configuration module
{
  pkgs,
  lib,
  config,
  ...
}: let
  codexSettings = {
    model = "gpt-5.1-codex";
    features = {
      streamable_shell = true;
      rmcp_client = true;
      unified_exec = true;
      view_image_tool = true;
      web_search_request = true;
    };
    shell_environment_policy = {
      "inherit" = "all";
      "set" = {CODEX_AGENT = "1";};
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

  tomlFormat = pkgs.formats.toml {};
  baseConfigFile = tomlFormat.generate "codex-config.nix-base" codexSettings;

  useXdgDirectories = config.home.preferXdgDirectories;
  xdgConfigHomeRelative = lib.removePrefix "${config.home.homeDirectory}/" config.xdg.configHome;
  codexHomeRelative =
    if useXdgDirectories
    then "${xdgConfigHomeRelative}/codex"
    else ".codex";
  codexHomeAbsolute =
    if useXdgDirectories
    then "${config.xdg.configHome}/codex"
    else "${config.home.homeDirectory}/.codex";

  baseFileRelative = "${codexHomeRelative}/config.nix-base.toml";
  baseFileAbsolute = "${codexHomeAbsolute}/config.nix-base.toml";
  liveFileAbsolute = "${codexHomeAbsolute}/config.toml";

  pythonMerge = pkgs.python3.withPackages (ps: [ps."tomli-w"]);

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
in {
  programs.codex = {
    enable = true;
    package = pkgs.codex;
    # Avoid letting the upstream module overwrite ~/.codex/config.toml.
    # The activation script below handles merging our desired settings.
  };

  home = {
    file."${baseFileRelative}".source = baseConfigFile;
    activation.codexConfig = lib.hm.dag.entryAfter ["writeBoundary"] mergeScript;
    sessionVariables = lib.mkIf useXdgDirectories {
      CODEX_HOME = "${config.xdg.configHome}/codex";
    };
  };
}
