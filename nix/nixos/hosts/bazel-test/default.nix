# bazel-test — minimal NixOS container for testing Bazel compatibility.
# Not a real host — used by devinfra/nixos_bazel_test/ to build a Docker image.
#
# Imports docker-image.nix which (together with docker-container.nix) provides
# system.build.tarball — a complete NixOS filesystem tarball for docker import.
# On container start, /init (systemd) runs NixOS activation: /etc, nix-ld,
# home-manager (bazelrc, direnv), etc. No manual wiring needed.
{ pkgs, ... }:
{
  imports = [
    "${pkgs.path}/nixos/modules/virtualisation/docker-image.nix"
    ../../modules/bazel-dev.nix
  ];

  # boot.isContainer is set by docker-container.nix (imported by docker-image.nix)
  networking.hostName = "bazel-test";
  users.users.root = {
    shell = pkgs.bash;
    isNormalUser = false;
  };

  # Container extras (a real NixOS host already has these)
  environment.systemPackages = with pkgs; [
    coreutils
    findutils
    gnugrep
    gnused
    gawk
    diffutils
    gnutar
    gzip
    xz
    which
    file
    patch
    curl
    cacert
    openssl
  ];

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  home-manager.useGlobalPkgs = true;
  home-manager.useUserPackages = true;
  home-manager.users.root = {
    imports = [ ../../../home/modules/nixos-bazel.nix ];
    programs.direnv = {
      enable = true;
      enableBashIntegration = true;
    };
    home.stateVersion = "25.11";
  };

  system.stateVersion = "25.11";
}
