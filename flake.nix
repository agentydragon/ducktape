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

    # Disabled: private input causes nixos-rebuild failures (SSH agent
    # forwarding, git-lfs not on root PATH). See nix/docs/private_flake_inputs.md
    # gaffer-private = {
    #   url = "git+ssh://git@github.com/agentydragon/gaffer-private?lfs=1";
    #   inputs.nixpkgs.follows = "nixpkgs";
    # };
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
        overlays = [
          # CLEANUP(2026-04-18): Remove once NixOS/nixpkgs#510952 merges into
          # nixos-unstable and we update the flake input.
          # The npm tarball ships vendor/seccomp/*/apply-seccomp at mode 0644;
          # NixOS preserves this into the store, breaking all sandboxed Bash calls.
          # Upstream: anthropics/claude-code#43367
          (final: prev: {
            claude-code = prev.claude-code.overrideAttrs (old: {
              postPatch = (old.postPatch or "") + ''
                chmod -f +x vendor/seccomp/*/apply-seccomp 2>/dev/null || true
              '';
            });
          })
        ];
      };

      # Shared home-manager args passed to every HM configuration.
      hmCommonArgs = {
        inherit
          nix-colors
          pkgsUnstable
          claude-plugins-official
          siderolabs-docs
          ;
        # gaffer-private disabled — see nix/docs/private_flake_inputs.md
        # inherit (inputs) gaffer-private;
        solarizedLight = nix-colors.colorSchemes.solarized-light;
        solarizedDark = nix-colors.colorSchemes.solarized-dark;
        terminalFont = {
          family = "JetBrainsMono Nerd Font";
          size = 11;
        };
        nixGLPackages = nixGL.packages.${system};
        ducktape-artifacts = artifacts;
        skills-tar = skillsUnpacked;
        sharedSkillsArgs = {
          inherit (pkgs)
            lib
            ;
          inherit
            pkgs
            siderolabs-docs
            ;
          skills-tar = skillsUnpacked;
        };
      };

      mkHome =
        {
          hostname,
          enableGui ? true,
          isNixOS ? false,
          isK8sWorker ? false,
          extraModules ? [ ],
        }:
        home-manager.lib.homeManagerConfiguration {
          inherit pkgs;

          modules = [
            inputs.sops-nix.homeManagerModules.sops
            # gaffer-private disabled — see nix/docs/private_flake_inputs.md
            # inputs.gaffer-private.homeManagerModules.google-drive
            ./nix/home/hosts/${hostname}.nix
            {
              _module.args = hmCommonArgs // {
                inherit
                  enableGui
                  isNixOS
                  isK8sWorker
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
                isNixOS = true;
                isK8sWorker = inlineHomeManager.isK8sWorker or false;
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
                  home-manager.sharedModules = [
                    inputs.sops-nix.homeManagerModules.sops
                    # gaffer-private disabled — see nix/docs/private_flake_inputs.md
                    # inputs.gaffer-private.homeManagerModules.google-drive
                  ];
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
      inherit (pkgs) lib;
      ducktapePkgs = import ./nix/packages { inherit lib pkgs artifacts; };
      # Dev tools shared between the devShell (local `nix develop` / direnv)
      # and Claude Code web (`nix profile install .#devtools`).
      # release.yml pushes this to attic so web installs are cache hits.
      # TODO: disable NLS on pre-commit's gitMinimal to drop ~31 MiB of
      # gettext + locale data. Blocked on slow rebuild (gitMinimal override
      # isn't in the binary cache, triggers 600+ derivation bootstrap chain).
      # See devinfra/claude/docs/devtools-closure-size.md for details.
      # Packages NOT needed on RBE workers (large, only for local/infra use).
      # Excluded from rbeToolPackages to keep the RBE image small.
      localOnlyPackages = [
        pkgs.rustfmt # 1GB (pulls full rustc via RPATH)
        pkgs.ansible # 650MB
      ];
      # System libraries matching RBE worker image (devinfra/rbe_image/Dockerfile).
      systemLibs = import ./nix/packages/system-libs.nix { inherit pkgs; };
      # Common dev tools shared by both Python and Rust hook implementations.
      devToolsCommon = [
        ducktapePkgs.bb
        ducktapePkgs.bbapi
        ducktapePkgs.bbr
        ducktapePkgs.ducktape-git-hooks
        ducktapePkgs.skills
        # Dev tools
        pkgs.pre-commit
        pkgs.bazelisk
        pkgs.nixfmt-rfc-style
        pkgs.statix
        pkgs.ruff
        pkgs.shfmt
        pkgs.buildifier
        pkgs.gofumpt
        ducktapePkgs.prettier
        pkgs.openssl
        # Codex setup materializes kubeconfig via devinfra/k8s/kubeconfig.py;
        # include a guaranteed Python runtime with pyyaml for that path.
        (pkgs.python3.withPackages (ps: [ ps.pyyaml ]))
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
        pkgs.ssh-to-age
        ducktapePkgs.kubernetes-mcp-server
      ];
      # Python claude-hook.
      devToolPackages = devToolsCommon ++ [ ducktapePkgs.claude-hooks ];
      devToolPackagesRust = devToolsCommon ++ [ ducktapePkgs.claude-hook-rs ];
    in
    {
      # Development shell — enter via `nix develop` or direnv (`use flake`).
      devShells.${system}.default = pkgs.mkShell {
        packages = devToolPackages ++ localOnlyPackages ++ systemLibs.packages;
        inherit (systemLibs) buildInputs;
        LD_LIBRARY_PATH = systemLibs.libraryPath;
      };

      packages.${system} = ducktapePkgs // {
        # Minimal CI package: just bb + sops (no claude-hooks wheel needed).
        citools = pkgs.symlinkJoin {
          name = "ducktape-citools";
          paths = [
            ducktapePkgs.bb
            pkgs.sops
          ];
        };
        # Installable package for `nix profile install .#devtools` (used by web_setup.sh).
        # Default: Python claude-hook. Use #devtools-rust for the Rust binary.
        devtools = pkgs.symlinkJoin {
          name = "ducktape-devtools";
          paths = devToolPackages ++ localOnlyPackages;
        };
        # Rust claude-hook variant (selected via `web_setup.sh --impl=rust`).
        devtools-rust = pkgs.symlinkJoin {
          name = "ducktape-devtools-rust";
          paths = devToolPackagesRust ++ localOnlyPackages;
        };
        # Lean devtools for RBE worker image (no rustfmt, ansible).
        rbetools = pkgs.symlinkJoin {
          name = "ducktape-rbetools";
          paths = devToolPackages;
        };
        # NixOS container tarball for docker import.
        # Build: nix build .#bazel-test-docker
        # Load:  docker import result ducktape-nixos-bazel
        # Run:   docker run --rm -it ducktape-nixos-bazel /init
        # Exec:  docker exec -it <container> bash -l
        bazel-test-docker = self.nixosConfigurations.bazel-test.config.system.build.tarball;
        # Nix-based RBE worker image (plain Docker, no NixOS/systemd).
        # Build: nix build .#nix-rbe-image
        # Load:  docker load < result
        nix-rbe-image = import ./x/nix_rbe_image { inherit pkgs; };
        # NixOS-based RBE worker (systemd, envfs, nix-ld).
        # Build: nix build .#nix-rbe-nixos
        # Load:  docker import result/tarball/*.tar.xz nix-rbe-nixos
        nix-rbe-nixos = self.nixosConfigurations.nix-rbe-worker.config.system.build.tarball;
        # Pre-built UEFI qcow2 VM images for Proxmox deployment.
        # Build: nix build .#wyrm2-image
        # Uses built-in system.build.images.qemu-efi (nixos-generators upstreamed in 25.05+).
        wyrm2-image = self.nixosConfigurations.wyrm2.config.system.build.images.qemu-efi;
        bootstrap-image = self.nixosConfigurations.bootstrap.config.system.build.images.qemu-efi;
      };

      homeConfigurations = {
        # NixOS VM
        nixos-vm = mkHome {
          hostname = "nixos-vm";
          enableGui = true;
          isNixOS = true;

        };

        # Atlas Proxmox VE host
        atlas = mkHome {
          hostname = "atlas";
          enableGui = true;
          isNixOS = false;

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
            isK8sWorker = true;

            module = ./nix/home/hosts/wyrm2.nix;
          };
        };

        rugged = mkNixos {
          hostname = "rugged";
          username = "agentydragon";
          # Physical machine - hardware config is in hosts/rugged/
          inlineHomeManager = {
            enableGui = true;
            isK8sWorker = true;
            module = ./nix/home/hosts/rugged.nix;
          };
        };

        iguana = mkNixos {
          hostname = "iguana";
          username = "agentydragon";
          # Physical machine (ThinkPad X1 Extreme)
          inlineHomeManager = {
            enableGui = true;
            isK8sWorker = true;

            module = ./nix/home/hosts/iguana.nix;
          };
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

        # NixOS-based RBE worker with full Bazel compat (envfs, nix-ld).
        nix-rbe-worker = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            ./x/nix_rbe_image/nixos.nix
          ];
        };

      };
    };
}
