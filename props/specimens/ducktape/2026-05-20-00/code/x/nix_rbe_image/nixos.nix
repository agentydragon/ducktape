# NixOS-based BuildBuddy RBE worker / runner image.
#
# Full NixOS container with systemd, envfs, nix-ld — all the NixOS Bazel
# compatibility machinery. Intended for use as both:
# - RBE container image (exec_properties container-image)
# - bb remote runner VM (runner_exec_properties container-image)
#
# The runner VM use case requires a working init (systemd) to set up envfs,
# nix-ld, and the environment. dockerTools images can't do this, but NixOS
# containers boot /init → systemd → activation scripts → everything works.
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
  virtualisation.docker.enable = true;

  # Firecracker's guest kernel may lack nftables support.
  networking.nftables.enable = false;

  environment.systemPackages = rbePackages;

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  system.stateVersion = "25.11";
}
