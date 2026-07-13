# ThinkPad X1 Extreme workstation
#
# Hardware:
# - CPU: Intel Core (CometLake-H)
# - GPU: Intel UHD Graphics (integrated) + NVIDIA GTX 1650 Ti Mobile (discrete, Optimus)
# - Storage: 954GB NVMe SSD (btrfs on LUKS, no LVM currently)
#   If OpenEBS LVM provisioner is needed later: shrink btrfs, shrink LUKS,
#   repartition to carve out an LVM PV, or re-do as LUKS → LVM → btrfs.
#
# Manual setup steps:
# - SSH keygen and copy to GitHub/GitLab
# - Transfer Ansible Vault password into libwallet
# - sops-nix age key generation (for when secrets are needed)
# - Nebula certs (will be added out-of-band later, via Google Keep or similar)
#
# Migration notes:
# - Migrated from Pop!_OS (ext4) to NixOS (btrfs) in-place
# - CUDA support for NVIDIA discrete GPU
{
  config,
  pkgs,
  lib,
  username,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
in
{
  imports = [
    ./hardware-configuration.nix
    ../../modules/gui.nix
    ../../modules/workstation.nix
    ../../modules/bazel
    ../../modules/system-inspection-sudo.nix
    ../../modules/claude-desktop.nix
    ../../modules/home-wifi.nix
    ../../modules/k8s-worker.nix
  ];

  # Passwordless sudo for system inspection commands
  ducktape.systemInspectionSudo.enable = true;

  # Claude Desktop Cowork sandboxed-microVM feature (QEMU firmware + virtiofsd
  # at the Debian /usr paths the app probes).
  ducktape.cowork.enable = true;

  ducktape.homeWifi.enable = true;

  # TODO: enable Attic substituter for cache.allegedly.works/{main,gaffer}.
  # Reader JWT is already auto-rotated into secrets/hosts/iguana-attic.yaml
  # by the attic-jwt-rotation CronJob (rotators.json entry exists). Wiring
  # mirrors wyrm2 (nix/nixos/hosts/wyrm2/default.nix:48–55) — import
  # ../../modules/attic-substituter.nix and:
  #   ducktape.attic-substituter = {
  #     enable = true;
  #     sopsFile = ../../../../secrets/hosts/iguana-attic.yaml;
  #   };

  ducktape.k8sWorker = {
    enable = true;
    nodeLabels = {
      "topology.kubernetes.io/region" = "roaming";
      "node.kubernetes.io/role" = "roaming";
    };
    nodeTaints = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
  };

  # Bluetooth
  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };

  # NVIDIA GPU configuration (GTX 1650 Ti Mobile, Optimus setup)
  hardware.nvidia = {
    modesetting.enable = true;
    powerManagement.enable = true; # Battery-friendly on laptop
    powerManagement.finegrained = false; # Disable finegrained for Optimus (use PRIME)
    open = false; # Proprietary driver for GTX 1650 Ti (open doesn't support it yet)
    nvidiaSettings = true; # GUI settings app
    package = config.boot.kernelPackages.nvidiaPackages.stable;

    # Optimus configuration - use NVIDIA on-demand
    prime = {
      offload = {
        enable = true;
        enableOffloadCmd = true; # Provides nvidia-offload command
      };
      # Bus IDs from lspci output:
      # 00:02.0 = Intel UHD Graphics
      # 01:00.0 = NVIDIA GTX 1650 Ti
      intelBusId = "PCI:0:2:0";
      nvidiaBusId = "PCI:1:0:0";
    };
  };
  services.xserver.videoDrivers = [ "nvidia" ];

  # CUDA support
  # Applications needing CUDA should use nvidia-offload:
  #   nvidia-offload <command>
  # Or set environment variable:
  #   __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia <command>
  hardware.graphics = {
    enable = true;
    enable32Bit = true; # For 32-bit apps/games
  };

  # Services
  services = {
    avahi = {
      enable = true;
      nssmdns4 = true; # mDNS resolution for .local hostnames
    };
    blueman.enable = true;
    fwupd.enable = true; # Firmware updates
    printing.enable = true;
    thermald.enable = true;

    # Lid/power button behavior
    logind.settings.Login = {
      HandleLidSwitch = "suspend";
      HandleLidSwitchExternalPower = "lock";
      HandlePowerKey = "suspend";
      HandlePowerKeyLongPress = "poweroff";
    };
  };

  # System packages
  environment.systemPackages = with pkgs; [
    powertop # Power consumption analysis
    telegram-desktop
    zoom-us
    # nvidia-offload wrapper is provided by hardware.nvidia.prime.offload.enableOffloadCmd
  ];

  # Local file sharing across devices (LAN)
  programs.localsend = {
    enable = true;
    openFirewall = true;
  };

  programs.steam.enable = true;

  # User configuration
  users.users.${username} = {
    shell = pkgs.zsh;
    extraGroups = [ "systemd-journal" ];
    openssh.authorizedKeys.keys = with keys; [
      iguana
      wyrm2
      atlas
      rugged
    ];
  };

  # Local desktop account only.  Deliberately omit `wheel` and SSH keys: Tesla
  # is not an administrator and cannot log in remotely.
  users.users.tesla = {
    isNormalUser = true;
    home = "/home/tesla";
    description = "tesla";
    shell = pkgs.zsh;
    extraGroups = [
      "networkmanager"
      "video"
      "audio"
    ];
  };

  # SPICE USB redirection helper (setuid root for USB device passthrough)
  security.wrappers.spice-client-glib-usb-acl-helper = {
    setuid = true;
    owner = "root";
    group = "root";
    source = "${pkgs.spice-gtk}/bin/spice-client-glib-usb-acl-helper";
  };

  # ThinkPad-specific optimizations
  # TrackPoint configuration (if desired)
  # services.xserver.libinput.enable = true;
  # services.xserver.libinput.touchpad.tapping = false; # Disable tap-to-click if preferred
}
