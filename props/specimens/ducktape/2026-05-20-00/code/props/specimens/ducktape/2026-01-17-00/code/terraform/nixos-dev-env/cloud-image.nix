# NixOS configuration for qcow2 cloud image with cloud-init support
{
  config,
  pkgs,
  lib,
  modulesPath,
  ...
}: {
  imports = [];

  # Enable cloud-init
  services.cloud-init = {
    enable = true;
    network.enable = true;
  };

  # Enable QEMU guest agent for Proxmox integration
  services.qemuGuest.enable = true;

  # Use systemd-networkd for cloud-init network configuration
  networking = {
    useDHCP = false;
    useNetworkd = true;
  };

  systemd.network = {
    enable = true;
    networks."10-cloud-init" = {
      matchConfig.Name = "en*";
      networkConfig = {
        DHCP = "yes";
        IPv6AcceptRA = true;
      };
    };
  };

  # Enable SSH
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };

  # Basic system packages
  environment.systemPackages = with pkgs; [
    vim
    wget
    curl
    git
    htop
    home-manager
    sudo
  ];

  # Enable sudo for wheel group
  security.sudo.enable = true;

  # Enable flakes and nix-command (needed for modern Nix/home-manager)
  nix.settings.experimental-features = ["nix-command" "flakes"];

  # Set NixOS release
  system.stateVersion = "24.11";

  # Allow unfree packages if needed
  nixpkgs.config.allowUnfree = true;
}
