# Experimental NixOS-based BuildBuddy RBE worker / runner image.
#
# Full NixOS container with systemd, envfs, nix-ld — all the NixOS Bazel
# compatibility machinery. This may be useful for RBE container and runner
# images, but BuildBuddy's Firecracker goinit currently does not run /init;
# see README.md before trying to use it.
#
# The runner VM use case requires a working init (systemd) to set up envfs,
# nix-ld, and the environment. dockerTools images can't do this, but normal
# NixOS container startup does.
#
# Build:  nix build .#nix-rbe-nixos
# Load:   docker import result/tarball/*.tar.xz nix-rbe-nixos
# Run:    docker run --rm --privileged -d nix-rbe-nixos /init
# Exec:   docker exec -it <container> bash -l
{ modulesPath, pkgs, ... }:
let
  rbePackages = import ./packages.nix { inherit pkgs; };
in
{
  imports = [
    (modulesPath + "/virtualisation/docker-image.nix")
    ../../nix/nixos/modules/bazel
  ];

  networking.hostName = "nix-rbe-worker";

  # BuildBuddy runs actions as "buildbuddy" user.
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

  # Docker — BB starts dockerd via init-dockerd on Firecracker workers.
  # docker_29: docker_28 was marked insecure (unmaintained since 2025-11).
  virtualisation.docker.enable = true;
  virtualisation.docker.package = pkgs.docker_29;

  # Firecracker's guest kernel may lack nftables support.
  networking.nftables.enable = false;

  environment.systemPackages = rbePackages;

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  system.stateVersion = "25.11";
}
