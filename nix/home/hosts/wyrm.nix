# Wyrm host-specific home-manager configuration
#
# To apply: home-manager switch --flake ~/code/ducktape#wyrm --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}:
let
  tana = pkgs.callPackage ../packages/tana.nix { };
in
{
  imports = [
    ../home.nix
    ../opencode
    ../codex
    ../modules/popos-bazel.nix
  ];

  # Wyrm-specific configuration (VM/desktop with full GUI)
  home.stateVersion = "24.05";

  home.packages = [
    tana
    pkgs.gemini-cli
  ];
  # TODO: Re-enable once k3s cluster is back up
  # services.google-drive.enable = true;

  # Disable screensaver and screen blanking (for VM/wyrm)
  dconf.settings = {
    "org/gnome/desktop/session" = {
      idle-delay = lib.hm.gvariant.mkUint32 0;
    }; # 0 = never
    "org/gnome/desktop/screensaver" = {
      lock-enabled = false;
    };
  };

  # Wyrm-specific pip configuration for tankshare storage
  # This creates ~/.config/pip/pip.conf to use shared cache
  # Only applies when /mnt/tankshare exists (virtiofs mount from atlas)
  xdg.configFile."pip/pip.conf" = lib.mkIf (builtins.pathExists "/mnt/tankshare") {
    text = ''
      [global]
      cache-dir = /mnt/tankshare/shared/pip-cache
    '';
  };

  # git-commit-ai configuration for local LLM
  xdg.configFile."ducktape/git_commit_ai.yml".text = ''
    model: gpt-oss-20b
    base_url: http://localhost:8000/v1
  '';

  # Dev toolchains on HDD (saves ~11GB on root SSD)
  # mkForce to override defaults from home.nix
  home.sessionVariables = {
    GOPATH = lib.mkForce "/wyrmhdd/go";
    RUSTUP_HOME = "/wyrmhdd/rustup";
    CARGO_HOME = "/wyrmhdd/cargo";
    PNPM_HOME = lib.mkForce "/wyrmhdd/pnpm";
    BUN_INSTALL = "/wyrmhdd/bun";
    UV_CACHE_DIR = "/wyrmhdd/uv-cache";
    HF_HOME = "/wyrmhdd/huggingface";
  };

  # Bazel: outputs on SSD (fast random I/O for runfiles symlinks),
  # repository cache on HDD (large tarballs, read sequentially, rarely written)
  home.file.".bazelrc".text = lib.mkBefore ''
    common --repository_cache=/wyrmhdd/bazel/repository_cache
  '';

  # Allow Claude Code to read Bazel repository cache on HDD
  programs.claude-code.extraAllowedReadDirs = [ "/wyrmhdd/bazel" ];
  # Allow Claude Code to write to Bazel output base on SSD
  programs.claude-code.additionalDirectories = [
    "~/.cache/bazel"
    "/wyrmhdd/bazel"
  ];
}
