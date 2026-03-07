# Bazel configuration for NixOS hosts (appended to ~/.bazelrc via home-manager).
# See investigations/nixos-bazel-bash/STATUS.md for full context.
{
  config,
  lib,
  ...
}:
{
  home.file.".bazelrc".text = lib.mkAfter ''
    # Override nixpkgs bazel_8's nix-store shell path for RBE compatibility
    build --shell_executable=/bin/bash
    # Fixed PATH for exec-config (host) tools only. Target-config actions use
    # Bazel's default PATH — hermetic toolchains resolve tools via runfiles,
    # and genrules use basic FHS utilities (tar, cp, echo) or $(location).
    # Not setting --action_env=PATH avoids leaking NixOS paths to RBE workers.
    build --host_action_env=PATH=/run/current-system/sw/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    # nix-ld env vars: only for local actions (host_action_env) and repo rules
    # (repo_env). NOT --action_env, because NIX_LD contains a nix-store path
    # (e.g. /nix/store/...-glibc/lib/ld-linux-x86-64.so.2) that doesn't exist
    # on RBE workers and would be actively wrong if anything tried to use it.
    build --host_action_env=NIX_LD
    build --host_action_env=NIX_LD_LIBRARY_PATH
    common --repo_env=NIX_LD
    common --repo_env=NIX_LD_LIBRARY_PATH
  '';
}
