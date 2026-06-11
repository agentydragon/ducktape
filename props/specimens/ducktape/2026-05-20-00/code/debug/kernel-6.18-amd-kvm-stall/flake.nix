# Standalone flake for KVM stall test VM images.
# Builds minimal NixOS qcow2 images with controllable kernel versions.
#
# Usage:
#   nix build .#kvm-test-6_12-image
#   scp result/disk.qcow2 root@atlas:/tmp/kvm-test-6_12.qcow2
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      sshKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFfzLZ7zOOMviYrrxeh1nSXdwu9uveSXr07EJI5NwFau agentydragon@popvm";

      # Base NixOS module for all test VMs
      baseModule =
        { lib, pkgs, ... }:
        {
          system.stateVersion = "25.11";

          # Boot — use defaults from disk-image.nix (systemd-boot + EFI).
          # VM must be created WITHOUT a separate efidisk so OVMF finds
          # the ESP on the NixOS image's own disk.
          boot.consoleLogLevel = 7;
          boot.plymouth.enable = false;
          boot.kernelParams = lib.mkDefault [
            "console=ttyS0,115200"
            "console=tty0"
          ];

          # Serial console for qm terminal access
          systemd.services."serial-getty@ttyS0".enable = true;
          boot.initrd.availableKernelModules = [
            "ahci"
            "xhci_pci"
            "virtio_pci"
            "sr_mod"
            "virtio_blk"
            "virtio_scsi"
          ];

          # QEMU guest
          services.qemuGuest.enable = true;
          # Skip spice-vdagentd — pulls in GTK/libcanberra which has build issues

          # Root filesystem — don't specify device; the image builder sets up
          # the correct labels/UUIDs and the initrd finds the root automatically.

          # SSH
          services.openssh = {
            enable = true;
            settings = {
              PasswordAuthentication = false;
              PermitRootLogin = "yes";
            };
          };

          # Users — both root and test user get SSH access
          users.users.root.openssh.authorizedKeys.keys = [ sshKey ];
          users.users.root.initialPassword = "test"; # for emergency console access
          users.users.test = {
            isNormalUser = true;
            extraGroups = [ "wheel" ];
            openssh.authorizedKeys.keys = [ sshKey ];
          };
          security.sudo.wheelNeedsPassword = false;

          # Test tools
          environment.systemPackages = [ pkgs.stress-ng ];

          # Size reduction — strip everything not needed for diagnosis
          nix.enable = false;
          documentation.enable = false;
          programs.command-not-found.enable = false;
          # services.udev.hwdb.enable not available in this nixpkgs
          systemd.coredump.enable = false;
          systemd.oomd.enable = false;
          fonts.fontconfig.enable = false;
          boot.supportedFilesystems = lib.mkForce [
            "ext4"
            "vfat"
          ];
          # Console auto-login for screenshot debugging
          services.getty.autologinUser = "test";

          # Networking — use systemd-networkd with a wildcard match so we don't
          # depend on a specific interface name. Matches any en* or eth* interface.
          networking.useDHCP = false;
          networking.useNetworkd = true;
          systemd.network.networks."10-lan" = {
            matchConfig.Name = "en* eth*";
            address = [ "10.0.200.1/16" ];
            gateway = [ "10.0.0.1" ];
            dns = [
              "1.1.1.1"
              "8.8.8.8"
            ];
          };
        };

      # Helper to create a test VM NixOS configuration
      mkTestVm =
        {
          ip,
          kernelPackages,
          hostname ? "kvm-test",
          extraKernelParams ? [ ],
        }:
        nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            baseModule
            {
              networking.hostName = hostname;
              boot.kernelPackages = kernelPackages;
              boot.kernelParams = extraKernelParams;
              systemd.network.networks."10-lan".address = [ "${ip}/16" ];
            }
          ];
        };

      # Test VM variants
      variants = {
        "kvm-test-6_12" = mkTestVm {
          ip = "10.0.200.1";
          kernelPackages = pkgs.linuxPackages_6_12;
        };
        "kvm-test-6_18" = mkTestVm {
          ip = "10.0.200.3";
          kernelPackages = pkgs.linuxPackages_6_18;
        };
        "kvm-test-6_19" = mkTestVm {
          ip = "10.0.200.4";
          kernelPackages = pkgs.linuxPackages_6_19;
        };
        "kvm-test-6_18-tsa-off" = mkTestVm {
          ip = "10.0.200.7";
          kernelPackages = pkgs.linuxPackages_6_18;
          extraKernelParams = [ "tsa=off" ];
        };
        "kvm-test-6_18-mitigations-off" = mkTestVm {
          ip = "10.0.200.8";
          kernelPackages = pkgs.linuxPackages_6_18;
          extraKernelParams = [ "mitigations=off" ];
        };
      };
    in
    {
      nixosConfigurations = variants;

      packages.${system} = builtins.mapAttrs (
        name: config: config.config.system.build.images.qemu-efi
      ) variants;
    };
}
