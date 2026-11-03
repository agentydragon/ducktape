{
  lib,
  ...
}:
{
  imports = [ ./vm.nix ];

  boot.initrd.enable = false;
  boot.kernelModules = [ ];
  boot.kernelPatches = [
    {
      name = "firecracker-built-in-devices";
      patch = null;
      structuredExtraConfig = with lib.kernel; {
        DEVTMPFS = lib.mkForce yes;
        DEVTMPFS_MOUNT = lib.mkForce yes;
        EXT4_FS = lib.mkForce yes;
        EXT4_FS_POSIX_ACL = lib.mkForce yes;
        ISO9660_FS = lib.mkForce yes;
        EROFS_FS = lib.mkForce yes;
        SERIAL_8250 = lib.mkForce yes;
        SERIAL_8250_CONSOLE = lib.mkForce yes;
        TMPFS = lib.mkForce yes;
        VIRTIO = lib.mkForce yes;
        VIRTIO_BLK = lib.mkForce yes;
        VIRTIO_MMIO = lib.mkForce yes;
        VIRTIO_NET = lib.mkForce yes;
        VIRTIO_VSOCKETS = lib.mkForce yes;
        VSOCKETS = lib.mkForce yes;
      };
    }
  ];

  boot.loader.systemd-boot.enable = lib.mkForce false;
  boot.loader.efi.canTouchEfiVariables = lib.mkForce false;
  boot.loader.grub.enable = lib.mkForce false;

  networking.firewall.allowedTCPPorts = [ 9090 ];
  networking.networkmanager.enable = lib.mkForce false;
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.networks."10-firecracker" = {
    matchConfig.Name = "eth0";
    address = [
      "192.0.2.2/30"
      "fd00:fc::2/64"
    ];
    routes = [
      { Gateway = "192.0.2.1"; }
      { Gateway = "fd00:fc::1"; }
    ];
    dns = [ "192.0.2.1" ];
    linkConfig.MTUBytes = "1280";
  };
  systemd.services.systemd-networkd = {
    requires = [ "agentplane-firecracker-thaw.service" ];
    after = [ "agentplane-firecracker-thaw.service" ];
  };
}
