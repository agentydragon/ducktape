# Wyrm host-specific home-manager configuration
#
# To apply: cd ~/code/ducktape/nix/home && home-manager switch --flake .#wyrm --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}: {
  imports = [
    ../home.nix
    ../opencode
    ../modules/popos-bazel.nix
  ];

  # Wyrm-specific configuration (VM/desktop with full GUI)
  home.stateVersion = "24.05";
  # TODO: Re-enable once k3s cluster is back up
  # services.google-drive.enable = true;

  # Disable screensaver and screen blanking (for VM/wyrm)
  dconf.settings = {
    "org/gnome/desktop/session" = {idle-delay = lib.hm.gvariant.mkUint32 0;}; # 0 = never
    "org/gnome/desktop/screensaver" = {lock-enabled = false;};
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

  # UV cache configuration for tankshare storage
  # Uses shared cache across VMs for efficiency (safe with virtiofs + UV's file locking)
  # Only applies when /mnt/tankshare exists (virtiofs mount from atlas)
  home.sessionVariables = lib.mkIf (builtins.pathExists "/mnt/tankshare") {
    UV_CACHE_DIR = "/mnt/tankshare/shared/uv-cache";
  };

  # Bazel output directory on HDD (avoids filling up root SSD)
  # Uses lib.mkBefore to prepend to content from popos-bazel.nix module
  home.file.".bazelrc".text = lib.mkBefore ''
    startup --output_user_root=/wyrmhdd/bazel
  '';
}
