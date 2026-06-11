# Workstation module - Docker, GUI apps, creative/productivity tools,
# and the full CLI diagnostic/debug toolkit. Imported by real workstation
# hosts (wyrm2/iguana/rugged). Kept out of `bootstrap` to shrink that image.
{
  config,
  pkgs,
  lib,
  username,
  ...
}:
{
  environment.systemPackages =
    (with pkgs; [
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

      # CLI editor + shell convenience
      neovim
      tmux
      mosh
      ripgrep
      tree
      pv
      home-manager

      # Disk + system inspection
      htop
      btop
      bottom
      procs
      iotop
      nix-du
      dust
      ncdu

      # Network diagnostics
      dig
      tcpdump
      iperf3
      conntrack-tools
      ethtool
      bpftools
      net-tools
      traceroute
      nmap
      iftop
      mtr
      bandwhich
      nethogs

      # System diagnostics / tracing
      gdb
      lsof
      ltrace
      strace
      usbutils
      pciutils
      acpi
      inotify-tools

      # Binary inspection / debugging
      file
      binutils
      elfutils
      patchelf
      valgrind
      heaptrack

      # Profiling / performance
      sysstat

      # Compression
      zip
      unzip

      # Serial/network utilities
      socat
      minicom
      zbar
      speedtest-cli

      # Secrets/credentials
      libsecret

      # PDF/OCR
      poppler-utils
      tesseract
    ])
    ++ [
      pkgs.perf
    ];

  # Docker
  virtualisation.docker = {
    enable = true;
    package = pkgs.docker_29;
    autoPrune.enable = true;
  };

  # Add user to docker group
  users.users.${username}.extraGroups = [ "docker" ];

}
