# Common host bootstrapping functionality
{lib}: rec {
  # Creates the self-bootstrapping flake configuration for a host
  mkHostFlake = hostname: ''
    {
      description = "Home Manager configuration for ${hostname}";

      inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
        home-manager = {
          url = "github:nix-community/home-manager";
          inputs.nixpkgs.follows = "nixpkgs";
        };
        ducktape.url = "github:agentydragon/ducktape/main";
      };

      outputs = { nixpkgs, home-manager, ducktape, ... }: {
        homeConfigurations."${hostname}" = home-manager.lib.homeManagerConfiguration {
          pkgs = nixpkgs.legacyPackages.x86_64-linux;
          modules = [ ducktape.packages.x86_64-linux.homeConfigurations.${hostname} ];
        };
      };
    }
  '';

  # Creates the standard host configuration pattern
  mkHostConfig = hostname: hostSpecificConfig:
    {
      imports = [../home.nix];

      # This host creates its own flake pointer
      xdg.configFile."home-manager/flake.nix".text = mkHostFlake hostname;
    }
    // hostSpecificConfig;
}
