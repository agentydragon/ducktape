# Hardware configuration for Proxmox VMs
# Provides VM-specific hardware settings. Filesystem mounts are included here
# with mkDefault so they can be overridden by /etc/nixos/hardware-configuration.nix
# when using --impure flag.
{
  config,
  lib,
  pkgs,
  modulesPath,
  ...
}:
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  # QEMU guest agent for Proxmox integration
  services.qemuGuest.enable = true;

  # SPICE agent: clipboard sharing + display resize.
  # Display is QXL-driven (NVIDIA GPUs are headless compute via VFIO).
  # Resize works via QXL DRM hotplug → mutter (GNOME handles it natively).
  # spice-autorandr is X11-only and not needed with GNOME/Wayland.
  services.spice-vdagentd.enable = true;

  # The NixOS module only starts spice-vdagentd (system daemon). The per-user
  # spice-vdagent process is also needed for display resize and clipboard.
  # It ships an XDG autostart .desktop, but GNOME 49 ignores it
  # (X-GNOME-Autostart-Phase is no longer honored). Use a systemd user service.
  # Clipboard sharing is broken on Wayland (upstream limitation, not NixOS).
  # See: https://github.com/NixOS/nixpkgs/issues/481078
  # TODO: Remove this once nixpkgs merges PR #266080 or equivalent upstream fix.
  systemd.user.services.spice-vdagent = {
    description = "SPICE guest agent (user session)";
    wantedBy = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.spice-vdagent}/bin/spice-vdagent -x";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  # Boot configuration for UEFI VMs
  boot.initrd.availableKernelModules = [
    "ahci"
    "xhci_pci"
    "virtio_pci"
    "sr_mod"
    "virtio_blk"
  ];
  boot.initrd.kernelModules = [ ];
  boot.kernelModules = [
    "kvm-intel"
    "kvm-amd"
  ];
  boot.extraModulePackages = [ ];

  # Filesystem placeholders - these allow the flake to evaluate locally.
  # On the VM, run nixos-rebuild with --impure to also import
  # /etc/nixos/hardware-configuration.nix which has the real disk UUIDs.
  # mkDefault ensures the generated config takes precedence.
  fileSystems."/" = lib.mkDefault {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  # Don't define /boot - not all images have a separate boot partition
  # The generated hardware-configuration.nix will define it if needed

  swapDevices = lib.mkDefault [ ];

  networking.useDHCP = lib.mkDefault true;
  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
