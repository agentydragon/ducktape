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
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };

      # CI-released artifact pins (npins/sources.json), updated by release.yml.
      # All entries are plain fetchurl; sha256 is SHA-256 SRI of the downloaded file.
      artifacts =
        let
          data = builtins.fromJSON (builtins.readFile ./npins/sources.json);
        in
        builtins.mapAttrs (
          _: spec:
          builtins.fetchurl {
            inherit (spec) url;
            sha256 = "sha256-${spec.sha256}";
          }
        ) data.pins;

      # skills.tar unpacked into a flat directory of skill subdirs.
      skillsUnpacked = pkgs.runCommand "skills" { } "mkdir $out && tar xf ${artifacts.skills} -C $out";

      pkgsUnstable = import nixpkgs-unstable {
        inherit system;
        config.allowUnfree = true;
      };

      # Shared home-manager args passed to every HM configuration.
      hmCommonArgs = {
        inherit
          nix-colors
          pkgsUnstable
          claude-plugins-official
          siderolabs-docs
          ;
        solarizedLight = nix-colors.colorSchemes.solarized-light;
        solarizedDark = nix-colors.colorSchemes.solarized-dark;
        terminalFont = {
          family = "JetBrainsMono Nerd Font";
          size = 11;
        };
        nixGLPackages = nixGL.packages.${system};
        ducktape-artifacts = artifacts;
        skills-tar = skillsUnpacked;
      };

      mkHome =
        {
          hostname,
          enableGui ? true,
          enableKube ? true,
          isNixOS ? false,
          isK8sWorker ? false,
          enableHeavyPackages ? true,
          extraModules ? [ ],
        }:
        home-manager.lib.homeManagerConfiguration {
          inherit pkgs;

          modules = [
            inputs.sops-nix.homeManagerModules.sops
            ./nix/home/hosts/${hostname}.nix
            {
              _module.args = hmCommonArgs // {
                inherit
                  enableGui
                  enableKube
                  isNixOS
                  isK8sWorker
                  enableHeavyPackages
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
          hmExtraSpecialArgs =
            if inlineHomeManager != null then
              hmCommonArgs
              // {
                enableGui = inlineHomeManager.enableGui or true;
                enableKube = inlineHomeManager.enableKube or false;
                isNixOS = true;
                isK8sWorker = inlineHomeManager.isK8sWorker or false;
                enableHeavyPackages = inlineHomeManager.enableHeavyPackages or false;
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
                  home-manager.sharedModules = [ inputs.sops-nix.homeManagerModules.sops ];
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
      devShells.${system}.default = pkgs.mkShell {
        packages = [ self.packages.${system}.web-session ];
      };

      # Packages exposed for nix-update and direct builds
      packages.${system} =
        let
          inherit (pkgs) lib;
          ducktapePkgs = import ./nix/packages { inherit lib pkgs artifacts; };
        in
        ducktapePkgs
        // {
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
              ducktapePkgs.claude-hooks
              ducktapePkgs.bbapi
              ducktapePkgs.skills
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
              pkgs.ansible
              # Infrastructure tools
              pkgs.gh
              pkgs.kubectl
              pkgs.fluxcd
              pkgs.kustomize
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
          # Firecracker dev VM rootfs (ext4) and kernel (vmlinux).
          # Build: nix build .#fc-dev-rootfs  /  nix build .#fc-dev-kernel
          fc-dev-rootfs = self.nixosConfigurations.fc-dev.config.system.build.ext4;
          fc-dev-kernel = self.nixosConfigurations.fc-dev.config.boot.kernelPackages.kernel;
        };

      homeConfigurations = {
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
            isK8sWorker = true;
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
            isK8sWorker = true;
            enableHeavyPackages = true;
            module = ./nix/home/hosts/rugged.nix;
          };
        };

        iguana = mkNixos {
          hostname = "iguana";
          username = "agentydragon";
          # Physical machine (ThinkPad X1 Extreme) - migrated from Pop!_OS (agentydragon)
          inlineHomeManager = {
            enableGui = true;
            enableKube = true;
            isK8sWorker = true;
            enableHeavyPackages = false;
            module = ./nix/home/hosts/iguana.nix;
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

        # Firecracker dev VM — NixOS rootfs for Bazel development.
        # Not a real host — produces ext4 image for Firecracker microVMs.
        fc-dev = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [ ./nix/nixos/hosts/fc_dev ];
        };
      };
    };
}
