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
    expected = 150;
  };

  # Verify allowed rules count
  test_allowed_count = {
    expr = builtins.length personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = 4;
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

  # Verify sample allowed rule structure
  test_sample_allowed_rule = {
    expr = builtins.head personalIntegration.programs.gemini-cli.policies.allowed-commands;
    expected = {
      toolName = "run_shell_command";
      commandPrefix = "git status";
      decision = "allow";
      priority = 300;
    };
  };
}
