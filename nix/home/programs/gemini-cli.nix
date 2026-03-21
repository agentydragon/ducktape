# Generic Gemini CLI home-manager module
#
# This is a standard home-manager module that could potentially be upstreamed.
# It provides declarative configuration for Gemini CLI.
#
# Example usage:
#   programs.gemini-cli = {
#     enable = true;
#     package = pkgs.gemini-cli;
#
#     settings = {
#       general = {
#         defaultApprovalMode = "default";
#         enableAutoUpdate = true;
#         sessionRetention.maxAge = 9999;
#       };
#       ui = {
#         theme = "dark";
#         autoThemeSwitching = true;
#       };
#       context = {
#         fileName = [ "AGENTS.md" "GEMINI.md" ];
#         includeDirectories = [ "/code" ];
#       };
#     };
#
#     policies = {
#       allowed-commands = [
#         {
#           toolName = "run_shell_command";
#           commandPrefix = "git status";
#           decision = "allow";
#           priority = 300;
#         }
#       ];
#       inspection-commands = [
#         {
#           toolName = "run_shell_command";
#           commandPrefix = "sudo lshw";
#           decision = "allow";
#           priority = 350;
#         }
#       ];
#     };
#   };
{
  config,
  lib,
  pkgs,
  siderolabs-docs,
  skills-tar,
  ...
}:
let
  cfg = config.programs.gemini-cli;

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
    home.file = (import ../skills/skills.nix { inherit lib pkgs siderolabs-docs skills-tar; }) ".gemini";
  };
}
