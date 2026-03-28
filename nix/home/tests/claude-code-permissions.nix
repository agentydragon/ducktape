# Test Claude Code permissions generation from SSOTs
#
# Run: nix-instantiate --eval --strict nix/home/tests/claude-code-permissions.nix

let
  pkgs = import <nixpkgs> { };
  inherit (pkgs) lib;

  # Import SSOTs
  inspection = import ../../lib/inspection-commands.nix { inherit lib; };
  allowed = import ../allowed-commands.nix;

  # Combine all commands from both SSOTs (same logic as claude_code/default.nix)
  allCommands = inspection.exports.noSudo ++ inspection.exports.sudo ++ allowed.noSudo;

  # Transform simple { type, cmd } → Bash() permission
  toBashPerm =
    entry:
    let
      suffix = if entry.type == "prefix" then ":*" else "";
    in
    "Bash(${entry.cmd}${suffix})";

  # Generate all Bash permissions
  bashPerms = map toBashPerm allCommands;

in
{
  # Verify total permission count
  test_total_count = {
    expr = builtins.length bashPerms;
    expected = 154;
  };

  # Verify first few permissions are correct
  test_first_permission = {
    expr = builtins.head bashPerms;
    expected = "Bash(lspci:*)";
  };

  # Verify a sudo permission exists
  test_has_sudo_permission = {
    expr = builtins.elem "Bash(sudo lshw:*)" bashPerms;
    expected = true;
  };

  # Verify an allowed command exists
  test_has_allowed_command = {
    expr = builtins.elem "Bash(git status:*)" bashPerms;
    expected = true;
  };
}
