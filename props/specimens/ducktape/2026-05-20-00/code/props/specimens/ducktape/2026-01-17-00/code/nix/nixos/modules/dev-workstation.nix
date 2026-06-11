# Dev workstation module - Docker, Tailscale, Chrome, gnome-terminal
{
  config,
  pkgs,
  lib,
  username,
  ...
}: {
  # System packages (GUI apps, tools that need system-level integration)
  environment.systemPackages = with pkgs; [
    gnome-terminal
    google-chrome
  ];

  # Docker
  virtualisation.docker = {
    enable = true;
    autoPrune.enable = true;
  };

  # Add user to docker group
  users.users.${username}.extraGroups = ["docker"];

  # Tailscale VPN
  services.tailscale.enable = true;
}
