# Base NixOS configuration shared by all VMs
{
  config,
  pkgs,
  lib,
  hostname,
  username,
  ...
}: {
  # Boot (UEFI with systemd-boot)
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Networking
  networking.hostName = hostname;
  networking.networkmanager.enable = true;

  # Nix settings - enable flakes
  nix = {
    settings = {
      experimental-features = ["nix-command" "flakes"];
      trusted-users = [username "root"];
      auto-optimise-store = true;
    };
  };

  # Allow unfree packages
  nixpkgs.config.allowUnfree = true;

  # User - password should be set after first boot with `passwd`
  users.users.${username} = {
    isNormalUser = true;
    home = "/home/${username}";
    description = username;
    extraGroups = ["wheel" "networkmanager" "video" "audio"];
  };

  # Sudo requires password by default (security)
  # Override in agent-sandbox modules if needed
  security.sudo.wheelNeedsPassword = true;

  # SSH
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # Essential packages
  environment.systemPackages = with pkgs; [
    git
    vim
    wget
    curl
    htop
    tmux
    home-manager
  ];

  system.stateVersion = "25.11";
}
