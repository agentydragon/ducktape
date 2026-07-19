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
  structuredSudoCommands = builtins.filter (
    entry: !builtins.isString entry
  ) inspection.exports.sudoDetailed;

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
    expected = builtins.length allCommands;
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

  # Verify structured sudo entries use normalized token-list args.
  test_sudo_detailed_args_are_lists = {
    expr = builtins.all (entry: builtins.isList entry.args) structuredSudoCommands;
    expected = true;
  };

  # Verify an allowed command exists
  test_has_allowed_command = {
    expr = builtins.elem "Bash(bazelisk test:*)" bashPerms;
    expected = true;
  };

  test_has_bazelisk_shutdown_permission = {
    expr = builtins.elem "Bash(bazelisk shutdown:*)" bashPerms;
    expected = true;
  };

  test_has_workspace_gc_permission = {
    expr = builtins.elem "Bash(workspace-gc:*)" bashPerms;
    expected = true;
  };

  test_has_gh_run_view_permission = {
    expr = builtins.elem "Bash(gh run view:*)" bashPerms;
    expected = true;
  };

  test_has_nix_eval_permission = {
    expr = builtins.elem "Bash(nix eval:*)" bashPerms;
    expected = true;
  };

  test_has_nix_build_permission = {
    expr = builtins.elem "Bash(nix build:*)" bashPerms;
    expected = true;
  };

  # nix develop wrapped commands
  test_has_prettier = {
    expr = builtins.elem "Bash(prettier:*)" bashPerms;
    expected = true;
  };

  test_has_nix_develop_prettier = {
    expr = builtins.elem "Bash(nix develop --command prettier:*)" bashPerms;
    expected = true;
  };

  test_has_nix_develop_c_prettier = {
    expr = builtins.elem "Bash(nix develop -c prettier:*)" bashPerms;
    expected = true;
  };

  test_has_pre_commit_run = {
    expr = builtins.elem "Bash(pre-commit run:*)" bashPerms;
    expected = true;
  };

  test_has_nix_develop_pre_commit_run = {
    expr = builtins.elem "Bash(nix develop -c pre-commit run:*)" bashPerms;
    expected = true;
  };

  test_has_talosctl_version = {
    expr = builtins.elem "Bash(talosctl version:*)" bashPerms;
    expected = true;
  };

  test_has_nix_develop_talosctl_version = {
    expr = builtins.elem "Bash(nix develop --command talosctl version:*)" bashPerms;
    expected = true;
  };
}
