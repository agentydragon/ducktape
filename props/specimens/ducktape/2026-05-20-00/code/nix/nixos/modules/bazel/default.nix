# NixOS module for Bazel development compatibility
#
# Addresses three NixOS-specific Bazel issues:
# 1. /bin/bash missing — envfs provides it (see below)
# 2. Empty PATH in sandbox actions — system.bazelrc installs /etc/bazel.bazelrc
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
  # Install NixOS-specific Bazel flags to /etc/bazel.bazelrc.
  # System-level bazelrc is read regardless of $HOME, so it works even when
  # Claude Code's sandbox overrides HOME.
  environment.etc."bazel.bazelrc".source = ./system.bazelrc;

  # envfs: FUSE mount at /bin and /usr/bin resolving binaries from PATH.
  # Bazel hardcodes /bin/bash for `bazel run` and run_shell() actions.
  # --shell_executable can't fix this: setting it to a Nix store path breaks
  # remote-executed actions on RBE (workers don't have /nix/store paths).
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
