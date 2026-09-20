# Experimental NixOS-based BuildBuddy Remote Runner image.
#
# Full NixOS container with systemd, envfs, nix-ld — all the NixOS Bazel
# compatibility machinery. BuildBuddy's Firecracker goinit currently does
# not run /init; see README.md before trying to use it as a remote runner image.
#
# A remote runner VM needs a working init (systemd) to set up envfs, nix-ld,
# and the environment. dockerTools images can't do this, but normal NixOS
# container startup does.
#
# Build:  nix build .#buildbuddy-remote-runner-nixos-image
# Load:   docker import result/tarball/*.tar.xz buildbuddy-remote-runner-nixos-image
# Run:    docker run --rm --privileged -d buildbuddy-remote-runner-nixos-image /init
# Exec:   docker exec -it <container> bash -l
{ modulesPath, pkgs, ... }:
let
  runnerPackages = import ../../../buildbuddy_nix_image_packages.nix { inherit pkgs; };
in
{
  imports = [
    (modulesPath + "/virtualisation/docker-image.nix")
    ../../../../nix/nixos/modules/bazel
  ];

  networking.hostName = "buildbuddy-remote-runner";

  # Keep the account name aligned with the base RBE container image.
  users.users.buildbuddy = {
    isNormalUser = true;
    home = "/home/buildbuddy";
    extraGroups = [
      "wheel"
      "docker"
    ];
    shell = pkgs.bash;
  };
  security.sudo.wheelNeedsPassword = false;

  # Docker — BuildBuddy can start dockerd via init-dockerd on Firecracker runners.
  # docker_29: docker_28 was marked insecure (unmaintained since 2025-11).
  virtualisation.docker.enable = true;
  virtualisation.docker.package = pkgs.docker_29;

  # Firecracker's guest kernel may lack nftables support.
  networking.nftables.enable = false;

  environment.systemPackages = runnerPackages;

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  system.stateVersion = "25.11";
}
