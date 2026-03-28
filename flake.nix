{
  description = "agentydragon's NixOS, home-manager, and development configurations";

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

    # Claude Code plugin marketplaces
    claude-plugins-official = {
      url = "github:anthropics/claude-plugins-official";
      flake = false;
    };

    # SideroLabs docs — source of truth for Talos/Omni AI agent skill
    # Update: nix flake update siderolabs-docs
    siderolabs-docs = {
      url = "github:siderolabs/docs";
      flake = false;
    };

    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixpkgs-unstable,
      home-manager,
      nix-colors,
      nixGL,
      claude-plugins-official,
      siderolabs-docs,
      ...
    }@inputs:
    let
      system = "x86_64-linux";

      # CI-released artifact pins (npins/sources.json), updated by release.yml.
      # "file" → fetchurl (wheels, binaries); "unpack" → fetchTarball (skills tar).
      artifacts =
        let
          data = builtins.fromJSON (builtins.readFile ./npins/sources.json);
          fetch =
            _name: spec:
            if spec.fetch == "unpack" then
              builtins.fetchTarball {
                inherit (spec) url;
                sha256 = spec.hash;
              }
            else
              builtins.fetchurl {
                inherit (spec) url;
                sha256 = spec.hash;
              };
        in
        builtins.mapAttrs fetch data.pins;

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
            ./nix/home/hosts/${hostname}.nix
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
                ducktape-wheel = artifacts.ducktape;
                claude-hooks-wheel = artifacts.claude-hooks;
                gterm-theme-wheel = artifacts.gterm-theme;
                bbapi-binary = artifacts.bbapi;
                skills-tar = artifacts.skills;
                inherit
                  claude-plugins-official
                  siderolabs-docs
                  ;
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
          # Inline home-manager config: if set, HM is activated during NixOS
          # activation (no hm-bootstrap.nix needed). Requires the HM host
          # config module path (e.g., ./nix/home/hosts/nixos-vm.nix).
          inlineHomeManager ? null,
        }:
        let
          # HM dependencies — only evaluated when inlineHomeManager is used
          # (Nix is lazy, so these won't be computed if not referenced)
          pkgsUnstable = import nixpkgs-unstable {
            inherit system;
            config.allowUnfree = true;
          };

          solarizedLight = nix-colors.colorSchemes.solarized-light;
          solarizedDark = nix-colors.colorSchemes.solarized-dark;

          hmExtraSpecialArgs =
            if inlineHomeManager != null then
              {
                enableGui = inlineHomeManager.enableGui or true;
                enableKube = inlineHomeManager.enableKube or false;
                isNixOS = true;
                isPopOS = false;
                enableHeavyPackages = inlineHomeManager.enableHeavyPackages or false;
                inherit
                  nix-colors
                  solarizedLight
                  solarizedDark
                  pkgsUnstable
                  ;
                nixGLPackages = nixGL.packages.${system};
                terminalFont = {
                  family = "JetBrainsMono Nerd Font";
                  size = 11;
                };
                ducktape-wheel = artifacts.ducktape;
                claude-hooks-wheel = artifacts.claude-hooks;
                gterm-theme-wheel = artifacts.gterm-theme;
                bbapi-binary = artifacts.bbapi;
                skills-tar = artifacts.skills;
                inherit
                  claude-plugins-official
                  siderolabs-docs
                  ;
              }
            else
              { };
        in
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
            ./nix/nixos/modules/base.nix
            ./nix/nixos/hosts/${hostname}
            home-manager.nixosModules.home-manager
            (
              if inlineHomeManager != null then
                {
                  home-manager.useGlobalPkgs = true;
                  home-manager.useUserPackages = true;
                  home-manager.extraSpecialArgs = hmExtraSpecialArgs;
                  home-manager.users.${username} = inlineHomeManager.module;
                }
              else
                {
                  home-manager.useGlobalPkgs = true;
                  home-manager.useUserPackages = true;
                }
            )
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
      # Development shell — same tools as web-session, usable via `direnv` (`use flake` in .envrc).
      devShells.${system}.default =
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };
        in
        pkgs.mkShell {
          packages = [ self.packages.${system}.web-session ];
        };

      # Packages exposed for nix-update and direct builds
      packages.${system} =
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };
        in
        rec {
          tana = pkgs.callPackage ./nix/home/packages/tana.nix { };
          gmail-mcp = pkgs.callPackage ./nix/home/packages/gmail-mcp.nix { };
          # ducktape wheel — provides ducktape-precommit (and git-commit-ai, difftree, etc.)
          # Used by CI pre-commit to satisfy the enforce-bazel-tests hook (language: system).
          ducktape = pkgs.callPackage ./nix/home/packages/ducktape.nix {
            ducktape-wheel = pkgs.runCommand "ducktape-0.1.0-py3-none-any.whl" { } ''
              cp ${artifacts.ducktape} $out
            '';
          };
          # Claude Code session hooks (claude-hook, claude-statusline, ducktape-precommit).
          claude-hooks = pkgs.callPackage ./nix/home/packages/claude-hooks.nix {
            claude-hooks-wheel = artifacts.claude-hooks;
          };
          gterm-theme = pkgs.callPackage ./nix/home/packages/gterm-theme.nix {
            gterm-theme-wheel = artifacts.gterm-theme;
          };
          bbapi = pkgs.callPackage ./nix/home/packages/bbapi.nix {
            bbapi-binary = artifacts.bbapi;
          };
          # Skills data: $out/share/claude-hooks/skills/ — deployed to ~/.claude/skills/.
          # Bundled into web-session; home-manager uses this via skills.nix module.
          skills = pkgs.runCommand "claude-hooks-skills" { } ''
            mkdir -p $out/share/claude-hooks/skills
            cp -r ${artifacts.skills}/. $out/share/claude-hooks/skills/
          '';
          # Shared dev tools — installed by web_setup.sh and used by the devShell.
          # Add tools here to make them available in both Claude Code web sessions
          # and local development (via direnv `use flake`).
          # release.yml pushes this to attic so installs are cache hits.
          # TODO: disable NLS on pre-commit's gitMinimal to drop ~31 MiB of
          # gettext + locale data. Blocked on slow rebuild (gitMinimal override
          # isn't in the binary cache, triggers 600+ derivation bootstrap chain).
          # See devinfra/claude/docs/web-session-closure-size.md for details.
          web-session = pkgs.symlinkJoin {
            name = "claude-web-session";
            paths = [
              # Repo-specific tools
              claude-hooks
              bbapi
              skills
              # Dev tools (also provided by .envrc via `use flake`)
              pkgs.pre-commit
              pkgs.bazelisk # TODO: ensure binary name matches what session start hook expects (no unconventional symlinks/aliases)
              pkgs.nixfmt-rfc-style
              pkgs.statix
              pkgs.mkcert
              pkgs.ruff
              pkgs.shfmt
              pkgs.buildifier
              pkgs.gofumpt
              # Infrastructure tools
              pkgs.gh
              pkgs.kubectl
              pkgs.fluxcd
              pkgs.kustomize
              pkgs.kubeseal
              pkgs.kubernetes-helm
              pkgs.kubeconform
              pkgs.opentofu
              pkgs.tflint
              pkgs.sops
            ];
          };
          # NixOS container tarball for docker import.
          # Build: nix build .#bazel-test-docker
          # Load:  docker import result ducktape-nixos-bazel
          # Run:   docker run --rm -it ducktape-nixos-bazel /init
          # Exec:  docker exec -it <container> bash -l
          bazel-test-docker = self.nixosConfigurations.bazel-test.config.system.build.tarball;
          # Pre-built UEFI qcow2 VM images for Proxmox deployment.
          # Build: nix build .#wyrm2-image
          # Uses built-in system.build.images.qemu-efi (nixos-generators upstreamed in 25.05+).
          wyrm2-image = self.nixosConfigurations.wyrm2.config.system.build.images.qemu-efi;
          bootstrap-image = self.nixosConfigurations.bootstrap.config.system.build.images.qemu-efi;
          k8s-worker-test-image =
            self.nixosConfigurations.k8s-worker-test.config.system.build.images.qemu-efi;
          # NixOS LXC tarball for Proxmox.
          # Build: nix build .#lxc-k8s-test-lxc
          # Upload: scp result/*.tar.xz root@atlas:/var/lib/vz/template/cache/
          lxc-k8s-test-lxc = self.nixosConfigurations.lxc-k8s-test.config.system.build.tarball;
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

        # Atlas Proxmox VE host
        atlas = mkHome {
          hostname = "atlas";
          enableGui = true;
          enableKube = true;
          isNixOS = false;
          enableHeavyPackages = false;
        };
      };

      nixosConfigurations = {
        wyrm2 = mkNixos {
          hostname = "wyrm2";
          username = "agentydragon";
          homeManagerHost = "wyrm2";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          inlineHomeManager = {
            enableGui = true;
            enableKube = false;
            enableHeavyPackages = false;
            module = ./nix/home/hosts/wyrm2.nix;
          };
        };

        rugged = mkNixos {
          hostname = "rugged";
          username = "agentydragon";
          # Physical machine - hardware config is in hosts/rugged/
          inlineHomeManager = {
            enableGui = true;
            enableKube = false;
            enableHeavyPackages = true;
            module = ./nix/home/hosts/rugged.nix;
          };
        };

        k8s-worker-test = mkNixos {
          hostname = "k8s-worker-test";
          username = "user";
          homeManagerHost = "nixos-vm";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
        };

        # NixOS LXC container on Proxmox — test k8s worker in LXC.
        # No hardwareModule — proxmox-lxc.nix is imported by the host config.
        lxc-k8s-test = mkNixos {
          hostname = "lxc-k8s-test";
          username = "agentydragon";
          homeManagerHost = "nixos-vm";
        };

        # Generic bootstrap NixOS — minimal SSH-able image for initial provisioning.
        bootstrap = mkNixos {
          hostname = "bootstrap";
          username = "agentydragon";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
        };

        # Minimal NixOS container for testing Bazel compatibility.
        # Not a real host — see nix/nixos/hosts/bazel-test/ for config.
        bazel-test = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            ./nix/nixos/hosts/bazel-test
            home-manager.nixosModules.home-manager
          ];
        };
      };
    };
}
