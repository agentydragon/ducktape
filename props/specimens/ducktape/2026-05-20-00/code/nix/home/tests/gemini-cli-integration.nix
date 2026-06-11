# Test Gemini CLI integration: SSOTs → Personal integration → Generic module
#
# Run: nix-instantiate --eval --strict nix/home/tests/gemini-cli-integration.nix

let
  pkgs = import <nixpkgs> { };
  inherit (pkgs) lib;

  # Mock minimal home-manager config structure
  config = {
    home.packages = [ ];
    xdg.configFile = { };
  };

  # Import the personal integration
  personalIntegration = import ../gemini_cli.nix { inherit config lib pkgs; };
  inspection = import ../../lib/inspection-commands.nix { inherit lib; };
  allowed = import ../allowed-commands.nix;

  expectedNixEvalRule = {
    toolName = "run_shell_command";
    commandPrefix = "nix eval";
    decision = "allow";
    priority = 300;
  };

  expectedNixBuildRule = {
    toolName = "run_shell_command";
    commandPrefix = "nix build";
    decision = "allow";
    priority = 300;
  };

in
{
  # Verify personal integration sets programs.gemini-cli.policies
  test_has_policies = {
    expr =
      builtins.hasAttr "programs" personalIntegration
      && builtins.hasAttr "gemini-cli" personalIntegration.programs
      && builtins.hasAttr "policies" personalIntegration.programs.gemini-cli;
    expected = true;
  };

  # Verify policies is a dict (not a list)
  test_policies_is_dict = {
    expr = builtins.isAttrs personalIntegration.programs.gemini-cli.policies;
    expected = true;
  };

  # Verify expected policy files exist
  test_policy_files = {
    expr = builtins.sort builtins.lessThan (
      builtins.attrNames personalIntegration.programs.gemini-cli.policies
    );
    expected = [
      "allowed-commands"
      "inspection-commands"
    ];
  };

  # Verify inspection rules count
  test_inspection_count = {
    expr = builtins.length personalIntegration.programs.gemini-cli.policies.inspection-commands;
    expected = builtins.length (inspection.exports.noSudo ++ inspection.exports.sudo);
  };

  # Verify allowed rules count
  test_allowed_count = {
    expr = builtins.length personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = builtins.length allowed.noSudo;
  };

  # Verify sample inspection rule structure
  test_sample_inspection_rule = {
    expr = builtins.head personalIntegration.programs.gemini-cli.policies.inspection-commands;
    expected = {
      toolName = "run_shell_command";
      commandPrefix = "lspci";
      decision = "allow";
      priority = 350;
    };
  };

  # Verify a representative allowed rule structure
  test_has_allowed_rule = {
    expr = builtins.elem {
      toolName = "run_shell_command";
      commandPrefix = "bazelisk test";
      decision = "allow";
      priority = 300;
    } personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = true;
  };

  test_has_nix_eval_rule = {
    expr = builtins.elem expectedNixEvalRule personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = true;
  };

  test_has_nix_build_rule = {
    expr = builtins.elem expectedNixBuildRule personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = true;
  };

  # Verify the first allowed rule still comes from the SSOT order
  test_first_allowed_rule = {
    expr = builtins.head personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = {
      toolName = "run_shell_command";
      commandPrefix = (builtins.head allowed.noSudo).cmd;
      decision = "allow";
      priority = 300;
    };
  };
}
