# Passwordless sudo for system inspection commands
#
# Grants NOPASSWD sudo access to read-only system inspection commands.
# Command list is imported from nix/lib/inspection-commands.nix (SSOT).
{
  config,
  lib,
  username,
  ...
}:
let
  inspection = import ../../lib/inspection-commands.nix { inherit lib; };

  # NixOS requires fully-qualified paths in sudoers
  # System packages are symlinked to /run/current-system/sw/bin/
  bin = "/run/current-system/sw/bin";

  # Transform command entry → sudoers rule
  # Input: { type = "prefix"|"exact"; cmd; args = [ ... ]; } OR plain string
  # Output: { command = "/full/path/to/cmd args"; options = ["NOPASSWD"]; }
  toSudoRule =
    entry:
    let
      # Handle both structured format and plain strings (for logViewingCommands)
      commandParts =
        if builtins.isString entry then
          lib.splitString " " entry # Plain string: split legacy sudoers-only log commands
        else
          [ entry.cmd ] ++ entry.args;

      # Extract base command for path resolution
      baseCmd = lib.head commandParts;
      fullCmd = "${bin}/${baseCmd}";

      argsList = lib.tail commandParts;
      args = lib.concatStringsSep " " argsList;

      # Determine match type
      isPrefix =
        if builtins.isString entry then
          false # Plain strings are exact match (logViewingCommands)
        else
          entry.type == "prefix";

      # Build sudoers rule command string
      cmdRule =
        if isPrefix then
          # Prefix match: allow trailing arguments
          if args != "" then "${fullCmd} ${args} *" else "${fullCmd} *"
        # exact
        else
        # Exact match: no trailing arguments
        if args != "" then
          "${fullCmd} ${args}"
        else
          "${fullCmd} \"\"";
    in
    {
      command = cmdRule;
      options = [ "NOPASSWD" ];
    };

  # Generate all sudo rules from normalized format
  allRules = map toSudoRule inspection.exports.sudoDetailed;
in
{
  options.ducktape.systemInspectionSudo = {
    enable = lib.mkEnableOption "passwordless sudo for system inspection commands";
  };

  config = lib.mkIf config.ducktape.systemInspectionSudo.enable {
    security.sudo.extraRules = [
      {
        users = [ username ];
        commands = allRules;
      }
    ];
  };
}
