{
  description = "agentydragon's NixOS and home-manager configurations";

  inputs = {
    # NixOS 25.11 stable release
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    # Unstable for packages that need frequent updates (e.g., claude-code)
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";

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

  outputs =
    {
      self,
      nixpkgs,
      nixpkgs-unstable,
      home-manager,
      nix-colors,
      claude-code-router,
      nixGL,
      ...
    }@inputs:
    let
      system = "x86_64-linux";

      mkHome =
        {
          hostname,
          enableGui ? true,
          enableKube ? true,
          isNixOS ? false,
          isPopOS ? false,
          enableHeavyPackages ? true,
          extraModules ? [ ],
        }:
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };

          pkgsUnstable = import nixpkgs-unstable {
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

          modules = [
            claude-code-router.homeManagerModules.claude-code-router
            ./home/hosts/${hostname}.nix
            {
              _module.args = {
                inherit
                  enableGui
                  enableKube
                  isNixOS
                  isPopOS
                  enableHeavyPackages
                  nix-colors
                  solarizedLight
                  solarizedDark
                  terminalFont
                  pkgsUnstable
                  ;
                nixGLPackages = nixGL.packages.${system};
              };
            }
          ]
          ++ extraModules;
        };

      mkNixos =
        {
          hostname,
          username ? "agentydragon",
          homeManagerHost ? hostname,
          hardwareModule ? null,
          extraModules ? [ ],
        }:
        nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs = {
            inherit
              inputs
              hostname
              username
              homeManagerHost
              ;
          };
          modules = [
            ./nixos/modules/base.nix
            ./nixos/hosts/${hostname}
            home-manager.nixosModules.home-manager
            {
              home-manager.useGlobalPkgs = true;
              home-manager.useUserPackages = true;
              # Home-manager config is applied separately via home-manager switch
            }
          ]
          ++ (
            if hardwareModule != null then
              [
                hardwareModule
                # For VMs: also try to import hardware-configuration.nix from /etc/nixos (requires --impure)
                (
                  if builtins.pathExists /etc/nixos/hardware-configuration.nix then
                    /etc/nixos/hardware-configuration.nix
                  else
                    { }
                )
              ]
            else
              [ ]
          )
          ++ extraModules;
        };
    in
    {
      # Packages exposed for nix-update and direct builds
      packages.${system} =
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };
        in
        {
          tana = pkgs.callPackage ./home/packages/tana.nix { };
          gmail-mcp = pkgs.callPackage ./home/packages/gmail-mcp.nix { };
          # NixOS container tarball for docker import.
          # Build: nix build path:./nix#bazel-test-docker
          # Load:  docker import result ducktape-nixos-bazel
          # Run:   docker run --rm -it ducktape-nixos-bazel /init
          # Exec:  docker exec -it <container> bash -l
          bazel-test-docker = self.nixosConfigurations.bazel-test.config.system.build.tarball;
        };

      homeConfigurations = {
        # Main laptop (ThinkPad X1 Extreme)
        agentydragon = mkHome {
          hostname = "agentydragon";
          enableGui = true;
          enableKube = true;
          isNixOS = false;
          isPopOS = true;
          enableHeavyPackages = false;
        };

        # GPD Win Max 2 laptop
        gpd = mkHome {
          hostname = "gpd";
          enableGui = true;
          enableKube = true;
          isNixOS = false;
          isPopOS = true;
          enableHeavyPackages = true;
        };

        # Wyrm desktop VM on atlas
        wyrm = mkHome {
          hostname = "wyrm";
          enableGui = true;
          enableKube = true;
          isNixOS = false;
          enableHeavyPackages = false;
        };

        # NixOS VM
        nixos-vm = mkHome {
          hostname = "nixos-vm";
          enableGui = true;
          enableKube = false;
          isNixOS = true;
          enableHeavyPackages = false;
        };

        # VPS server (minimal, no GUI)
        vps = mkHome {
          hostname = "vps";
          enableGui = false;
          enableKube = false;
          isNixOS = false;
          enableHeavyPackages = false;
        };

        # Dell Rugged 12 tablet
        rugged = mkHome {
          hostname = "rugged";
          enableGui = true;
          enableKube = false;
          isNixOS = true;
          enableHeavyPackages = true;
        };

        # Atlas Proxmox VE host
        atlas = mkHome {
          hostname = "atlas";
          enableGui = true;
          enableKube = false;
          isNixOS = false;
          enableHeavyPackages = false;
        };
      };

      nixosConfigurations = {
        wyrm2 = mkNixos {
          hostname = "wyrm2";
          username = "user";
          homeManagerHost = "nixos-vm";
          hardwareModule = ./nixos/modules/vm-hardware.nix;
        };

        rugged = mkNixos {
          hostname = "rugged";
          username = "agentydragon";
          homeManagerHost = "rugged";
          # Physical machine - hardware config is in hosts/rugged/
        };

        k8s-worker-test = mkNixos {
          hostname = "k8s-worker-test";
          username = "user";
          homeManagerHost = "nixos-vm";
          hardwareModule = ./nixos/modules/vm-hardware.nix;
        };

        # Minimal NixOS container for testing Bazel compatibility.
        # Not a real host — see nixos/hosts/bazel-test/ for config.
        bazel-test = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            ./nixos/hosts/bazel-test
            home-manager.nixosModules.home-manager
          ];
        };
      };
    };
}
