# fc-dev — NixOS configuration for Firecracker dev VMs.
# Not a real host — produces an ext4 rootfs image for Firecracker microVMs.
#
# Build rootfs: nix build .#fc-dev-rootfs
# Build kernel: nix build .#fc-dev-kernel
#
# The VM runs systemd as init, starts sshd on boot, and includes the full
# Bazel development toolchain from bazel-dev.nix.
{
  modulesPath,
  pkgs,
  lib,
  ...
}:
{
  imports = [
    ../../modules/bazel-dev.nix
    (modulesPath + "/profiles/minimal.nix")
    ./make-rootfs.nix
  ];

  # Firecracker boots the kernel directly — no bootloader.
  boot.loader.grub.enable = false;
  boot.initrd.enable = false;
  boot.isContainer = false;
  boot.kernelParams = [
    "console=ttyS0"
    "reboot=k"
    "panic=1"
  ];

  # Minimal kernel modules for virtio (Firecracker's device model).
  boot.initrd.availableKernelModules = lib.mkForce [ ];
  boot.kernelModules = [
    "virtio_blk"
    "virtio_net"
    "virtio_pci"
    "virtio_mmio"
  ];

  networking.hostName = "fc-dev";
  # Static network config — set by the VM pod entrypoint via kernel cmdline
  # or DHCP. For simplicity, use a static config matching the TAP subnet.
  networking.useDHCP = false;
  networking.interfaces.eth0 = {
    ipv4.addresses = [
      {
        address = "10.0.0.2";
        prefixLength = 30;
      }
    ];
  };
  networking.defaultGateway = {
    address = "10.0.0.1";
    interface = "eth0";
  };
  networking.nameservers = [
    "8.8.8.8"
    "1.1.1.1"
  ];

  # SSH access — the only way into the VM.
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };
  # Placeholder key — real key injected via process_api CreateProcess or
  # written to ~/.ssh/authorized_keys via WS command after boot.
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAA-placeholder-replaced-at-runtime"
  ];

  # Dev tools beyond bazel-dev.nix
  environment.systemPackages = with pkgs; [
    bazelisk
    python313
    clang
    openssh
    curl
    wget
    jq
    htop
    # Dev headers for pip wheel builds
    pkg-config
    openssl.dev
    cairo.dev
    dbus.dev
  ];

  # systemd: fast boot, no unnecessary services.
  services.getty.autologinUser = "root";
  systemd.services."serial-getty@ttyS0".enable = true;

  # Firecracker VMs have limited resources — keep it lean.
  documentation.enable = false;
  programs.command-not-found.enable = false;

  # Nix itself (for nix-shell, nix build inside the VM).
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  system.stateVersion = "25.11";
}
