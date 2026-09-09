# What is true of every NixOS host in this flake, and nothing else. Anything that assumes a
# person logs in -- the operator account, sudo, NetworkManager, editors -- lives in operator.nix,
# which hosts with a human import. Deeper workstation tooling is in workstation.nix.
{
  config,
  pkgs,
  lib,
  inputs,
  hostname,
  ...
}:
{
  imports = [ inputs.sops-nix.nixosModules.sops ];
  # Boot (UEFI with systemd-boot)
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Networking
  networking.hostName = hostname;

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

  # Zsh as default shell
  programs.zsh.enable = true;

  # SSH
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # Bare minimum for every host including `bootstrap`: what scripts and the Nix machinery reach
  # for. Editors and interactive extras are in operator.nix; diagnostics in workstation.nix.
  environment.systemPackages = with pkgs; [
    git
    curl
    openssl
  ];

  system.stateVersion = "25.11";
}
