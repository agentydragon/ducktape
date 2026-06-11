# Passwordless sudo for system inspection commands
#
# Grants NOPASSWD sudo access to read-only system inspection commands.
# Command list is imported from nix/lib/inspection-commands.nix (SSOT).
{
  config,
  lib,
  username,
  ...
}: let
  inspectionCommands = import ../../lib/inspection-commands.nix;

  # NixOS requires fully-qualified paths in sudoers
  # System packages are symlinked to /run/current-system/sw/bin/
  bin = "/run/current-system/sw/bin";

  # Convert command list to sudo rules
  # For commands where any arguments are safe
  anyArgsRules =
    map (cmd: {
      command = "${bin}/${cmd}";
      options = ["NOPASSWD"];
    })
    inspectionCommands.sudoAnyArgsCommands;

  # For exact subcommands: flatten { cmd, args = [list] } into individual rules
  # Includes logViewingCommands (same structure, separate in SSOT because Claude Code omits them)
  # Empty string arg ("") means command with no arguments
  exactRules = lib.flatten (map (
      entry:
        map (arg: {
          command = "${bin}/${entry.cmd}${
            if arg == ""
            then " \"\""
            else " ${arg}"
          }";
          options = ["NOPASSWD"];
        })
        entry.args
    )
    (inspectionCommands.sudoExactSubcommands ++ inspectionCommands.logViewingCommands));

  # For wildcard subcommands: flatten { cmd, prefixes = [list] } into rules with wildcard
  wildcardRules = lib.flatten (map (
      entry:
        map (prefix: {
          command = "${bin}/${entry.cmd} ${prefix} *";
          options = ["NOPASSWD"];
        })
        entry.prefixes
    )
    inspectionCommands.sudoWildcardSubcommands);

  allRules = anyArgsRules ++ exactRules ++ wildcardRules;
in {
  options.ducktape.systemInspectionSudo = {
    enable = lib.mkEnableOption "passwordless sudo for system inspection commands";
  };

  config = lib.mkIf config.ducktape.systemInspectionSudo.enable {
    security.sudo.extraRules = [
      {
        users = [username];
        commands = allRules;
      }
    ];
  };
}
