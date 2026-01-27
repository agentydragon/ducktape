# Wyrm host-specific home-manager configuration
#
# To apply: cd ~/code/ducktape/nix/home && home-manager switch --flake .#wyrm --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}:
{
  imports = [
    ../home.nix
    ../opencode
    ../codex
    ../modules/popos-bazel.nix
  ];

  # Wyrm-specific configuration (VM/desktop with full GUI)
  home.stateVersion = "24.05";
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

  # Bazel output directory on HDD (avoids filling up root SSD)
  # Uses lib.mkBefore to prepend to content from popos-bazel.nix module
  home.file.".bazelrc".text = lib.mkBefore ''
    startup --output_user_root=/wyrmhdd/bazel
  '';

  # Allow Claude Code to access Bazel output directory
  # Read: for test logs, build outputs, etc.
  # Write: for pre-commit hooks running bazel format, etc.
  programs.claude-code.extraAllowedReadDirs = [ "/wyrmhdd/bazel" ];
  programs.claude-code.additionalDirectories = [ "/wyrmhdd/bazel" ];
}
