# Generate Claude Code permission patterns from system inspection commands
#
# Imports from the SSOT (nix/lib/inspection-commands.nix) and generates
# Bash permission strings for Claude Code's settings.permissions.allow list.
{ lib }:
let
  inspection = import ../../lib/inspection-commands.nix { inherit lib; };

  # Combine all inspection commands (already in uniform { type, cmd } format)
  allCommands = inspection.exports.noSudo ++ inspection.exports.sudo;

  # Transform simple { type, cmd } → Bash() permission
  toBashPerm =
    entry:
    let
      suffix = if entry.type == "prefix" then ":*" else "";
    in
    "Bash(${entry.cmd}${suffix})";
in
{
  # All inspection-related permissions for Claude Code
  # Note: logViewingCommands intentionally omitted from Claude Code permissions
  # Claude Code uses prefix matching only, so we cannot restrict to specific paths.
  # These commands get passwordless sudo via NixOS module but not auto-allow in Claude Code.
  permissions = map toBashPerm allCommands;
}
