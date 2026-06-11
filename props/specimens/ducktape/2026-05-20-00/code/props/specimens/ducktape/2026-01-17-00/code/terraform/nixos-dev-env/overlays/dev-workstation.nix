# Dev workstation overlay - full development environment
# Similar to wyrm VM: Docker, Tailscale, Chrome, gnome-terminal
{
  config,
  pkgs,
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

  # Add user to docker group (assumes single user matching config)
  users.users.user.extraGroups = ["docker"];

  # Tailscale VPN
  services.tailscale.enable = true;
}
