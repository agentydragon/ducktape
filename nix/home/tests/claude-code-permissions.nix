{ pkgs }:

let
  inherit (pkgs) lib;

  claudeModule = import ../claude_code/default.nix {
    config.programs.claude-code = {
      additionalDirectories = [ ];
      extraAllowedReadDirs = [ ];
      extraAllowedWebFetchDomains = [ ];
    };
    inherit lib pkgs;
    pkgsUnstable = { };
    claude-plugins-official = { };
    sharedSkillsArgs = { };
  };
  permissions = claudeModule.config.programs.claude-code.settings.permissions;
  permissionsJson = pkgs.writeText "claude-settings-permissions.json" (builtins.toJSON permissions);
in
pkgs.runCommand "claude-code-permissions"
  {
    nativeBuildInputs = [ pkgs.jq ];
  }
  ''
    jq -e '
      (.allow | index("Bash(git status:*)")) != null
      and (.allow | index("Bash(pwd)")) != null
      and (.allow | index("Bash(pwd:*)")) == null
      and (.allow | index("Bash(lspci:*)")) != null
    ' '${permissionsJson}' >/dev/null
    touch "$out"
  ''
