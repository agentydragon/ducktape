# Single source of truth for heavy packages
# These are installed:
# - Via NixOS system config on NixOS systems (using the module in nix/nixos/)
# - Via home-manager on non-NixOS systems
# - Not at all on minimal systems (e.g., atlas, vps, agentydragon)
#
# This list contains packages that are:
# - Large in download/install size
# - Used for creative/productivity work (not essential for development)
# - Better shared system-wide on NixOS to save space
{
  heavyPackages = pkgs:
    with pkgs; [
      # Creative/CAD
      freecad
      openscad
      xournalpp # Note-taking and PDF annotation

      # Graphics/Audio editing
      gimp
      krita
      inkscape # Vector graphics editor
      audacity

      # Development & Analysis
      vscode # IDE (~400MB)
      wireshark # Network analyzer

      # Media & Downloads
      vlc # Full-featured media player
      transmission_4-gtk # BitTorrent client

      # Web browsers
      google-chrome # Chrome browser

      # Communication (Electron apps)
      discord # Gaming/community chat
      element-desktop # Matrix client

      # Future additions could include:
      # blender      # 3D modeling
      # darktable    # Photo workflow
      # kdenlive     # Video editing
      # ardour       # DAW
      # libreoffice  # Office suite
      # obs-studio   # Streaming/recording
      # steam        # Gaming platform
    ];
}
