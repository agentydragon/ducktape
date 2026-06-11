{
  description = "Home Manager configurations for agentydragon's machines";

  inputs = {
    # NixOS 25.11 stable release
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    # Home Manager tracking 25.11 release
    home-manager = {
      url = "github:nix-community/home-manager/release-25.11";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # nix-colors for colorscheme support
    nix-colors.url = "github:Misterio77/nix-colors";

    # nixGL for OpenGL support in non-NixOS systems
    # NOTE: nixGL requires --impure flag when building because it detects NVIDIA driver versions
    # at evaluation time using builtins.currentTime (not available in pure mode).
    # Build with: nix build --impure .#homeConfigurations.HOSTNAME.activationPackage
    # Or: home-manager switch --impure --flake .#HOSTNAME
    nixGL = {
      url = "github:guibou/nixGL/main";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    claude-code-router.url = "github:agentydragon/claude-code-router/2b7c2ca764f74fd80a6c8b85495df7793282758d";
  };

  outputs = {
    self,
    nixpkgs,
    home-manager,
    nix-colors,
    claude-code-router,
    nixGL,
  }: let
    system = "x86_64-linux";

    # Helper to create home configuration
    mkHome = {
      hostname,
      enableGui ? true,
      enableKube ? true,
      isNixOS ? false, # Whether this is a NixOS system (uses system packages for heavy apps)
      enableHeavyPackages ? true, # Whether to install heavy creative/CAD packages
      extraModules ? [],
    }: let
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };

      solarizedLight = nix-colors.colorSchemes.solarized-light;
      solarizedDark = nix-colors.colorSchemes.solarized-dark;

      terminalFont = {
        family = "JetBrainsMono Nerd Font";
        size = 11;
      };
    in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        modules =
          [
            claude-code-router.homeManagerModules.claude-code-router
            ./hosts/${hostname}.nix
            {
              _module.args = {
                inherit
                  enableGui
                  enableKube
                  isNixOS
                  enableHeavyPackages
                  nix-colors
                  solarizedLight
                  solarizedDark
                  terminalFont
                  ;
                nixGLPackages = nixGL.packages.${system};
              };
            }
          ]
          ++ extraModules;
      };
  in {
    homeConfigurations = {
      # Main laptop (ThinkPad X1 Extreme)
      agentydragon = mkHome {
        hostname = "agentydragon";
        enableGui = true;
        enableKube = true;
        isNixOS = false;
        enableHeavyPackages = false;
      };

      # GPD Win Max 2 laptop
      gpd = mkHome {
        hostname = "gpd";
        enableGui = true;
        enableKube = true;
        isNixOS = false;
        enableHeavyPackages = true;
      };

      # Wyrm desktop VM on atlas
      wyrm = mkHome {
        hostname = "wyrm";
        enableGui = true;
        enableKube = true;
        isNixOS = false;
        enableHeavyPackages = true;
      };

      # NixOS VM
      nixos-vm = mkHome {
        hostname = "nixos-vm";
        enableGui = true;
        enableKube = false;
        isNixOS = true; # NixOS system
        enableHeavyPackages = false; # Lightweight VM - no heavy packages
      };

      # VPS server (minimal, no GUI)
      vps = mkHome {
        hostname = "vps";
        enableGui = false;
        enableKube = false;
        isNixOS = false;
        enableHeavyPackages = false; # Server doesn't need creative apps
      };

      # Dell Rugged 12 tablet
      rugged = mkHome {
        hostname = "rugged";
        enableGui = true;
        enableKube = false; # TODO: set true and provision kubeconfig when needed
        isNixOS = true; # NixOS system - heavy packages via system config
        enableHeavyPackages = true;
      };

      # Atlas Proxmox VE host
      atlas = mkHome {
        hostname = "atlas";
        enableGui = true;
        enableKube = false;
        isNixOS = false;
        enableHeavyPackages = false; # Minimal Proxmox host - no creative apps
      };
    };
  };
}
