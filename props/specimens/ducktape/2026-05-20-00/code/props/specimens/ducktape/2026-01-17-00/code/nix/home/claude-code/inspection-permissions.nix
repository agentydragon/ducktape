# Generate Claude Code permission patterns from system inspection commands
#
# Imports from the SSOT (nix/lib/inspection-commands.nix) and generates
# Bash permission strings for Claude Code's settings.permissions.allow list.
{lib}: let
  inspectionCommands = import ../../lib/inspection-commands.nix;

  # Helper to generate Bash permission strings from command names
  # Exact match (no wildcard)
  mkBashPerms = cmds: map (cmd: "Bash(${cmd})") cmds;
  mkBashPermsSudo = cmds: map (cmd: "Bash(sudo ${cmd})") cmds;
  # Prefix match (with :* wildcard)
  mkBashPermsWildcard = cmds: map (cmd: "Bash(${cmd}:*)") cmds;
  mkBashPermsSudoWildcard = cmds: map (cmd: "Bash(sudo ${cmd}:*)") cmds;

  noSudoInspectionCommands = inspectionCommands.noSudoCommands;
  sudoAnyArgsInspectionCommands = inspectionCommands.sudoAnyArgsCommands;

  # Convert structured subcommands to string format for Claude Code permissions
  # Flatten { cmd, args = [list] } into individual "cmd arg" strings
  # Empty string arg ("") means command with no arguments (exact match)
  sudoSpecificSubcommandsExact = lib.flatten (map (e:
    map (arg: "${e.cmd}${
      if arg == ""
      then ""
      else " ${arg}"
    }")
    e.args)
  inspectionCommands.sudoExactSubcommands);

  # Flatten { cmd, prefixes = [list] } into "cmd prefix" strings for wildcard matching
  sudoSpecificSubcommandsWildcard = lib.flatten (map (e:
    map (prefix: "${e.cmd} ${prefix}")
    e.prefixes)
  inspectionCommands.sudoWildcardSubcommands);
in {
  # All inspection-related permissions for Claude Code
  # Note: logViewingCommands intentionally omitted from Claude Code permissions
  # Claude Code uses prefix matching only, so we cannot restrict to specific paths.
  # These commands get passwordless sudo via NixOS module but not auto-allow in Claude Code.
  permissions =
    mkBashPermsWildcard noSudoInspectionCommands
    ++ mkBashPermsSudoWildcard sudoAnyArgsInspectionCommands
    ++ mkBashPermsSudo sudoSpecificSubcommandsExact
    ++ mkBashPermsSudoWildcard sudoSpecificSubcommandsWildcard;
}
