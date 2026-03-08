# NixOS module for Bazel development compatibility
#
# Addresses three NixOS-specific Bazel issues:
# 1. /bin/bash missing — nixpkgs bazel_8 is already patched for this
# 2. Empty PATH in sandbox actions — handled by home-manager nixos-bazel.nix
# 3. Dynamically-linked Bazel-downloaded toolchains — nix-ld provides the linker stub
#
# See debug/nixos_bazel_bash/README.md for details.
{
  config,
  pkgs,
  lib,
  ...
}:
{
  # nix-ld: provides /lib64/ld-linux-x86-64.so.2 stub so dynamically-linked
  # binaries Bazel downloads (python-build-standalone, rustc, node) can run.
  programs.nix-ld.enable = true;

  # Development packages needed for Bazel builds
  environment.systemPackages = with pkgs; [
    # Bazel (nixpkgs version, already patched for NixOS /bin/bash issue)
    bazel_8
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
