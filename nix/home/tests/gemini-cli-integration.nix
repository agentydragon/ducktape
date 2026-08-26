{ pkgs }:

let
  inherit (pkgs) lib;

  evaluated = lib.evalModules {
    specialArgs = {
      inherit pkgs;
      sharedSkillsArgs = {
        inherit lib pkgs;
        siderolabs-docs = ./.;
        skills-tar = ./.;
      };
    };
    modules = [
      (
        { lib, ... }:
        {
          options = {
            programs.gemini-cli = {
              enable = lib.mkEnableOption "Gemini CLI";
              settings = lib.mkOption {
                type = lib.types.attrsOf lib.types.anything;
                default = { };
              };
            };
            home = {
              file = lib.mkOption {
                type = lib.types.attrsOf lib.types.anything;
                default = { };
              };
              packages = lib.mkOption {
                type = lib.types.listOf lib.types.anything;
                default = [ ];
              };
            };
            xdg.configFile = lib.mkOption {
              type = lib.types.attrsOf lib.types.anything;
              default = { };
            };
          };
        }
      )
      ../programs/gemini-cli.nix
      ../gemini_cli.nix
    ];
  };
  allowedPolicy = evaluated.config.xdg.configFile."gemini/policies/allowed-commands.toml".source;
  inspectionPolicy =
    evaluated.config.xdg.configFile."gemini/policies/inspection-commands.toml".source;
in
pkgs.runCommand "gemini-cli-integration"
  {
    nativeBuildInputs = [ pkgs.python3 ];
  }
  ''
    python - '${allowedPolicy}' '${inspectionPolicy}' <<'PY'
    import sys
    import tomllib

    def rules(path: str) -> list[dict[str, object]]:
        with open(path, "rb") as policy_file:
            return tomllib.load(policy_file)["rule"]

    allowed = rules(sys.argv[1])
    inspection = rules(sys.argv[2])

    assert {
        "toolName": "run_shell_command",
        "commandPrefix": "git status",
        "decision": "allow",
        "priority": 300,
    } in allowed
    assert {
        "toolName": "run_shell_command",
        "commandPrefix": "pwd",
        "decision": "allow",
        "priority": 300,
    } in allowed
    assert {
        "toolName": "run_shell_command",
        "commandPrefix": "lspci",
        "decision": "allow",
        "priority": 350,
    } in inspection
    PY
    touch "$out"
  ''
