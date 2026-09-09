# Hardware settings shared by every VM guest here, Proxmox and KubeVirt alike: qemu-guest
# profile, virtio initrd modules, DHCP, and filesystem placeholders.
#
# Filesystem mounts use mkDefault so /etc/nixos/hardware-configuration.nix can override them
# under --impure. Guests someone actually looks at additionally import vm-desktop.nix.
{
  config,
  lib,
  modulesPath,
  ...
}:
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  # QEMU guest agent. Proxmox integration, and what backs KubeVirt's AgentConnected condition.
  services.qemuGuest.enable = true;

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
