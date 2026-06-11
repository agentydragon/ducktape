# NixOS Configuration for ${hostname}
# Managed by Terraform

{ config, pkgs, lib, ... }:

{
  imports = [
    ./hardware-configuration.nix
    ./overlay.nix  # VM-specific overlay (dev-workstation, agent-sandbox, etc.)
  ];

  # Boot (UEFI with systemd-boot)
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Networking
  networking.hostName = "${hostname}";
  networking.networkmanager.enable = true;

  # Time zone
  time.timeZone = "UTC";

  # Nix settings - enable flakes
  nix = {
    settings = {
      experimental-features = [ "nix-command" "flakes" ];
      trusted-users = [ "${username}" "root" ];
      auto-optimise-store = true;
    };
  };

  # Allow unfree packages
  nixpkgs.config.allowUnfree = true;

  # User
  users.users.${username} = {
    isNormalUser = true;
    home = "/home/${username}";
    description = "${username}";
    extraGroups = [ "wheel" "networkmanager" "video" "audio" ];
    hashedPassword = null;  # No password
%{ if ssh_public_key != "" ~}
    openssh.authorizedKeys.keys = [ "${ssh_public_key}" ];
%{ endif ~}
  };

  # Passwordless sudo
  security.sudo.wheelNeedsPassword = false;

%{ if enable_gui ~}
  # GNOME Desktop
  services.xserver = {
    enable = true;
    displayManager.gdm = {
      enable = true;
      autoSuspend = false;
    };
    desktopManager.gnome.enable = true;
  };

  # Auto-login
  services.displayManager.autoLogin = {
    enable = true;
    user = "${username}";
  };

  # Workaround for auto-login with GNOME
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;

  # GNOME settings
  services.gnome.gnome-keyring.enable = true;
  programs.dconf.enable = true;

  # Disable screen lock
  programs.dconf.profiles.user.databases = [{
    settings = {
      "org/gnome/desktop/session" = {
        idle-delay = lib.gvariant.mkUint32 0;
      };
      "org/gnome/desktop/screensaver" = {
        lock-enabled = false;
        lock-delay = lib.gvariant.mkUint32 0;
      };
    };
  }];
%{ endif ~}

  # SSH
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # QEMU guest agent for Proxmox integration
  services.qemuGuest.enable = true;

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

  # Environment variables (Proxmox credentials + custom vars)
  environment.variables = {
%{ for key, value in env_vars ~}
    ${key} = "${value}";
%{ endfor ~}
  };

  system.stateVersion = "${nixos_channel == "unstable" ? "24.11" : nixos_channel}";
}
