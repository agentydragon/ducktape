# Base NixOS configuration shared by all VMs
{
  config,
  pkgs,
  lib,
  inputs,
  hostname,
  username,
  ...
}:
{
  imports = [ inputs.sops-nix.nixosModules.sops ];
  # Boot (UEFI with systemd-boot)
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Networking
  networking.hostName = hostname;
  networking.networkmanager.enable = lib.mkDefault true;

  # Timezone
  time.timeZone = "America/Los_Angeles";

  # Nix settings - enable flakes
  nix = {
    settings = {
      experimental-features = [
        "nix-command"
        "flakes"
        # fetch-closure: needed by nix/packages/gaffer.nix to substitute
        # private drivefs/drivectl closures from cache.allegedly.works/gaffer
        # without resorting to `builtins.storePath` (which requires --impure).
        "fetch-closure"
      ];
      trusted-users = [
        username
        "root"
      ];
      auto-optimise-store = true;
    };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 14d";
    };
  };

  boot.loader.systemd-boot.configurationLimit = 10;

  # Allow unfree packages
  nixpkgs.config.allowUnfree = true;

  # User - password should be set after first boot with `passwd`
  users.users.${username} = {
    isNormalUser = true;
    home = "/home/${username}";
    description = username;
    extraGroups = [
      "wheel"
      "networkmanager"
      "video"
      "audio"
    ];
  };

  # Sudo requires password by default (security)
  # Override in agent-sandbox modules if needed
  security.sudo.wheelNeedsPassword = true;
  security.sudo.extraConfig = lib.mkAfter ''
    # Show asterisks while typing sudo passwords.
    Defaults pwfeedback
  '';

  # Zsh as default shell
  programs.zsh.enable = true;

  # Allow reading kernel logs without sudo
  boot.kernel.sysctl."kernel.dmesg_restrict" = 0;

  # SSH
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # Bare-minimum packages shared by every host (including `bootstrap`).
  # Diagnostics, editors, profiling, etc. live in workstation.nix and are
  # imported by real workstation hosts only.
  environment.systemPackages = with pkgs; [
    git
    vim
    curl
    wget
    openssl
  ];

  system.stateVersion = "25.11";
}
