# Test system inspection sudoers rendering from the shared command SSOT.
#
# Run: nix-instantiate --eval --strict nix/nixos/tests/system-inspection-sudo.nix

let
  pkgs = import <nixpkgs> { };
  inherit (pkgs) lib;

  inspection = import ../../lib/inspection-commands.nix { inherit lib; };

  evaluated = lib.evalModules {
    specialArgs = {
      username = "agentydragon";
    };
    modules = [
      (
        { lib, ... }:
        {
          options.security.sudo.extraRules = lib.mkOption {
            type = lib.types.anything;
            default = [ ];
          };
        }
      )
      ../modules/system-inspection-sudo.nix
      (_: {
        ducktape.systemInspectionSudo.enable = true;
      })
    ];
  };

  inherit (builtins.head evaluated.config.security.sudo.extraRules) commands;
  sudoRule = command: {
    inherit command;
    options = [ "NOPASSWD" ];
  };
  hasSudoRule = command: builtins.elem (sudoRule command) commands;
in
{
  test_rule_count_matches_ssot = {
    expr = builtins.length commands;
    expected = builtins.length inspection.exports.sudoDetailed;
  };

  test_prefix_no_args_allows_trailing_args = {
    expr = hasSudoRule "/run/current-system/sw/bin/lshw *";
    expected = true;
  };

  test_exact_no_args_forbids_trailing_args = {
    expr = hasSudoRule "/run/current-system/sw/bin/nvidia-smi \"\"";
    expected = true;
  };

  test_exact_single_arg_has_no_trailing_wildcard = {
    expr = hasSudoRule "/run/current-system/sw/bin/fdisk -l";
    expected = true;
  };

  test_exact_multi_token_args_have_no_trailing_wildcard = {
    expr = hasSudoRule "/run/current-system/sw/bin/ip -s addr show";
    expected = true;
  };

  test_prefix_multi_token_args_allow_trailing_args = {
    expr = hasSudoRule "/run/current-system/sw/bin/nmcli connection show *";
    expected = true;
  };

  test_legacy_log_viewing_string_is_path_restricted = {
    expr = hasSudoRule "/run/current-system/sw/bin/tail -f /var/log/*";
    expected = true;
  };

  test_legacy_log_viewing_string_does_not_gain_extra_wildcard = {
    expr = hasSudoRule "/run/current-system/sw/bin/tail -f /var/log/* *";
    expected = false;
  };
}
