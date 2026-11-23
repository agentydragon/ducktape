# Policy and skill extensions to the former Home Manager Gemini CLI module.
# See ../home.nix for the disabled imports and ../gemini_cli.nix for the consumer.
{
  config,
  lib,
  pkgs,
  sharedSkillsArgs,
  ...
}:
let
  cfg = config.programs.gemini-cli;
  mkSkills = import ../skills.nix sharedSkillsArgs;

  # Use pkgs.formats.toml for proper TOML generation
  tomlFormat = pkgs.formats.toml { };
in
{
  # This module EXTENDS home-manager's built-in programs.gemini-cli module
  # by adding the 'policies' option (which the upstream module doesn't provide).
  # All other options (enable, package, settings, commands, etc.) come from home-manager.

  options.programs.gemini-cli = {
    # Don't redefine enable/package - those come from home-manager's module

    policies = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.listOf (
          lib.types.submodule {
            options = {
              toolName = lib.mkOption {
                type = lib.types.str;
                description = "Tool name (e.g., 'run_shell_command')";
              };
              commandPrefix = lib.mkOption {
                type = lib.types.str;
                description = "Command prefix to match";
              };
              decision = lib.mkOption {
                type = lib.types.enum [
                  "allow"
                  "deny"
                  "ask_user"
                ];
                description = "Policy decision";
              };
              priority = lib.mkOption {
                type = lib.types.int;
                description = "Rule priority (higher = evaluated first)";
              };
            };
          }
        )
      );
      default = { };
      description = "Gemini CLI policy files. Each attribute name becomes a separate TOML file in ~/.config/gemini/policies/";
      example = lib.literalExpression ''
        {
          allowed-commands = [
            { toolName = "run_shell_command"; commandPrefix = "git status"; decision = "allow"; priority = 300; }
          ];
          inspection-commands = [
            { toolName = "run_shell_command"; commandPrefix = "sudo lshw"; decision = "allow"; priority = 350; }
          ];
        }
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # Only handle policy files - everything else (settings.json, package, etc.)
    # is handled by home-manager's built-in gemini-cli module
    xdg.configFile = lib.mapAttrs' (
      name: rules:
      lib.nameValuePair "gemini/policies/${name}.toml" {
        source = tomlFormat.generate "${name}.toml" { rule = rules; };
      }
    ) cfg.policies;

    # Deploy skills to ~/.gemini/skills/ (local + external, shared with Claude Code)
    home.file = mkSkills { prefix = ".gemini"; };
  };
}
