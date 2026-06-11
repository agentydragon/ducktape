# Workstation module - Docker, GUI apps, creative/productivity tools
{
  config,
  pkgs,
  lib,
  username,
  ...
}:
{
  # System packages (GUI apps, tools that need system-level integration)
  environment.systemPackages = with pkgs; [
    gnome-terminal
    google-chrome

    # Creative/CAD
    freecad
    openscad
    xournalpp

    # Graphics/Audio editing
    gimp
    krita
    inkscape
    audacity

    # Development & Analysis
    vscode
    wireshark

    # Media & Downloads
    vlc
    transmission_4-gtk

    # Communication (Electron apps)
    discord
    element-desktop
  ];

  # Docker
  virtualisation.docker = {
    enable = true;
    autoPrune.enable = true;
  };

  # Add user to docker group
  users.users.${username}.extraGroups = [ "docker" ];

}
