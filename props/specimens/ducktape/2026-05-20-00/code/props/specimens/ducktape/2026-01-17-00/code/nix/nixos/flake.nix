{
  description = "NixOS configurations for agentydragon's machines";

  inputs = {
    # NixOS 25.11 stable release
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    home-manager = {
      url = "github:nix-community/home-manager/release-25.11";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Import the home-manager flake for shared config
    ducktape-home = {
      url = "github:agentydragon/ducktape?dir=nix/home&ref=devel";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    self,
    nixpkgs,
    home-manager,
    ducktape-home,
    ...
  } @ inputs: let
    system = "x86_64-linux";

    # Helper to create NixOS configuration
    mkNixos = {
      hostname,
      username ? "agentydragon",
      homeManagerHost ? hostname,
      # For VMs: pass ./modules/vm-hardware.nix
      # For physical machines: null (uses hosts/${hostname}/hardware-configuration.nix)
      hardwareModule ? null,
      extraModules ? [],
    }:
      nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {inherit inputs hostname username homeManagerHost;};
        modules =
          [
            ./modules/base.nix
            ./hosts/${hostname}
            home-manager.nixosModules.home-manager
            {
              home-manager.useGlobalPkgs = true;
              home-manager.useUserPackages = true;
              # Home-manager config will be applied separately via flake
            }
          ]
          ++ (
            if hardwareModule != null
            then [
              hardwareModule
              # For VMs: also try to import hardware-configuration.nix from /etc/nixos (requires --impure)
              (
                if builtins.pathExists /etc/nixos/hardware-configuration.nix
                then /etc/nixos/hardware-configuration.nix
                else {}
              )
            ]
            else []
          )
          ++ extraModules;
      };
  in {
    nixosConfigurations = {
      wyrm2 = mkNixos {
        hostname = "wyrm2";
        username = "user";
        homeManagerHost = "nixos-vm";
        hardwareModule = ./modules/vm-hardware.nix;
      };

      rugged = mkNixos {
        hostname = "rugged";
        username = "agentydragon";
        homeManagerHost = "rugged";
        # Physical machine - hardware config is in hosts/rugged/
      };
    };
  };
}
