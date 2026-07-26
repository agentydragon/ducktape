# NixOS module for Bazel development compatibility
#
# Addresses three NixOS-specific Bazel issues:
# 1. /bin/bash missing — envfs provides it (see below)
# 2. Empty PATH in sandbox actions — system.bazelrc installs /etc/bazel.bazelrc
# 3. Dynamically-linked Bazel-downloaded toolchains — nix-ld provides the linker stub
#
# KNOWN LIMIT: #3 only holds while actions execute somewhere FHS. nix-ld is
# *environment-based*, and every ruleset that scrubs its environment defeats it
# (rules_python runs `exec env -`; rules_js's js_binary wrapper scrubs before exec'ing
# node), each needing its own passthrough, from an open-ended list. Our NixOS hosts don't
# feel this because `bbr` runs actions on FHS RBE workers — local execution on Nix glibc is
# where it bites, and it is not fully fixable there. Do not build a container base this way;
# use an FHS base with a Nix tool closure. Measured 2026-07-26; see
# debug/nixos_bazel_bash/README.md "Issue 4".
#
# See debug/nixos_bazel_bash/README.md for details.
{
  config,
  pkgs,
  lib,
  ...
}:
{
  # Install NixOS-specific local Bazel flags to /etc/bazel.bazelrc.
  # System-level bazelrc is read regardless of $HOME, so it works even when
  # Claude Code's sandbox overrides HOME.
  environment.etc."bazel.bazelrc".source = ./system.bazelrc;

  # envfs remains useful for software that hardcodes FHS executable paths.
  # Bazel itself uses the explicit local shell in system.bazelrc; an RBE config
  # can override that with the remote worker's /bin/bash.
  services.envfs.enable = true;

  # nix-ld: provides /lib64/ld-linux-x86-64.so.2 stub so dynamically-linked
  # binaries Bazel downloads (python-build-standalone, rustc, node) can run.
  programs.nix-ld.enable = true;

  # Development packages needed for Bazel builds
  environment.systemPackages = with pkgs; [
    # Build essentials
    gcc
    gnumake
    binutils
    patchelf
    # Direnv for .envrc support
    direnv
    # SCM
    git
    # Python (rules_python bootstrap)
    python3
  ];
}
