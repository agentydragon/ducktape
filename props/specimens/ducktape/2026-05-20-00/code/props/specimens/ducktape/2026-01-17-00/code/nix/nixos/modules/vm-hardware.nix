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
}: {
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  # QEMU guest agent for Proxmox integration
  services.qemuGuest.enable = true;

  # Boot configuration for UEFI VMs
  boot.initrd.availableKernelModules = ["ahci" "xhci_pci" "virtio_pci" "sr_mod" "virtio_blk"];
  boot.initrd.kernelModules = [];
  boot.kernelModules = ["kvm-intel" "kvm-amd"];
  boot.extraModulePackages = [];

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

  swapDevices = lib.mkDefault [];

  # Networking - use DHCP
  networking.useDHCP = lib.mkDefault true;

  # Platform
  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
