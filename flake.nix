{
  description = "agentydragon's NixOS, home-manager, and development configurations";

  inputs = {
    # NixOS 26.05 stable release
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # OpenClaw's Nix packaging. The image output below consumes its pinned
    # gateway derivation and adds the small public-coder runtime toolset.
    nix-openclaw = {
      url = "github:openclaw/nix-openclaw";
      inputs.nixpkgs.follows = "nixpkgs-unstable";
    };

    # Unstable for packages that need frequent updates (e.g., claude-code)
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Master for packages that temporarily need changes newer than
    # nixos-unstable. Keep consumers narrow instead of moving whole hosts to
    # nixpkgs master.
    nixpkgs-master.url = "github:NixOS/nixpkgs/master";

    # Home Manager tracking 26.05 release
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
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

    # Declarative Nix environment for Android (phone), via a Termux fork.
    # aarch64-linux only; see nix/droid/README.md.
    nix-on-droid = {
      url = "github:nix-community/nix-on-droid";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixpkgs-unstable,
      nixpkgs-master,
      home-manager,
      nix-colors,
      nixGL,
      nix-openclaw,
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
      # Keep the experimental BuildBuddy Remote Runner NixOS configuration and
      # output definitions with the experiment; the root flake only registers them.
      buildbuddyRemoteRunnerNixosOutputs = import ./devinfra/buildbuddy_remote_runner/x/nixos/flake-outputs.nix {
        inherit nixpkgs self system;
      };
      artifactData = builtins.fromJSON (builtins.readFile ./nix/artifact-pins.json);
      rawArtifactOverrides = builtins.getEnv "DUCKTAPE_ARTIFACT_OVERRIDES";
      artifactOverrides =
        if rawArtifactOverrides == "" then { } else builtins.fromJSON rawArtifactOverrides;
      # Keep archive names as evaluation-time metadata. Deriving them from
      # artifact store paths later would include Nix's hash prefix.
      artifactFilenames = builtins.mapAttrs (
        name: spec:
        if artifactOverrides ? ${name} then baseNameOf artifactOverrides.${name} else baseNameOf spec.url
      ) artifactData.pins;

      # Keep the developer Ruff binary aligned with the repository's pinned
      # Python and Bazel toolchains while nixpkgs catches up.
      ruffLatest = pkgs.stdenvNoCC.mkDerivation {
        pname = "ruff";
        version = "0.16.7";
        src = pkgs.fetchurl {
          url = "https://github.com/astral-sh/ruff/releases/download/0.16.7/ruff-x86_64-unknown-linux-gnu.tar.gz";
          hash = "sha256-c4lMe3yaU/1m7XFes6HsZQd/MWMo43cFepi9t/y6AyY=";
        };
        dontBuild = true;
        dontConfigure = true;
        dontCheck = true;
        installPhase = ''
          install -Dm755 ruff $out/bin/ruff
        '';
        meta.mainProgram = "ruff";
      };

      # CI-released artifact pins (nix/artifact-pins.json), updated by sync-pins.yml.
      # Use Nixpkgs fetchurl derivations, not builtins.fetchurl: hosts behind
      # restricted egress can substitute these fixed-output paths from Attic
      # instead of doing evaluator-time downloads from GitHub Releases.
      #
      # PR-time override: DUCKTAPE_ARTIFACT_OVERRIDES (JSON object of pin
      # name -> absolute file path) swaps the fetched wheel for a local one.
      # Set by .github/workflows/nix-wheel-check.yml so the imports check runs
      # against the PR's freshly-built wheel instead of the last released pin —
      # that is what catches "wheel forgot a package" regressions like the
      # gmail_api / ducktape_pkg drift in #2669. Requires --impure (getEnv).
      # Empty in normal use; behaviour is identical to the pre-override flake.
      artifacts = builtins.mapAttrs (
        name: spec:
        if artifactOverrides ? ${name} then
          # Preserve the local artifact's basename. Wheel installers validate
          # the archive filename against its embedded .dist-info directory.
          builtins.path {
            path = /. + artifactOverrides.${name};
            name = artifactFilenames.${name};
          }
        else
          pkgs.fetchurl {
            inherit (spec) url;
            name =
              let
                asset = artifactFilenames.${name};
              in
              if pkgs.lib.hasSuffix ".whl" asset || pkgs.lib.hasSuffix ".zip" asset then asset else "source";
            hash = "sha256-${spec.sha256}";
          }
      ) artifactData.pins;

      # Each skill ships as its own `skill-<name>` release artifact. Assemble the
      # per-skill `.skill` zips (each already rooted under `<name>/`) into one flat
      # directory of `<name>/` subdirs. Registry: skills/skills_registry.json.
      skillsUnpacked =
        let
          registry = builtins.fromJSON (builtins.readFile ./skills/skills_registry.json);
          skills = builtins.filter (skill: builtins.hasAttr "skill-${skill.name}" artifacts) registry.skills;
        in
        pkgs.runCommand "skills" { nativeBuildInputs = [ pkgs.libarchive ]; } (
          "mkdir $out\n"
          + pkgs.lib.concatMapStringsSep "\n" (s: "bsdtar -xf ${artifacts."skill-${s.name}"} -C $out") skills
        );

      pkgsUnstable = import nixpkgs-unstable {
        inherit system;
        config.allowUnfree = true;
      };

      pkgsMaster = import nixpkgs-master {
        inherit system;
        config.allowUnfree = true;
      };

      # aarch64 nixpkgs instance for nix-on-droid (the phone). Everything else in
      # this flake is pinned to `system` (x86_64-linux) above.
      pkgsAarch64 = import nixpkgs {
        system = "aarch64-linux";
        config.allowUnfree = true;
      };

      # Shared home-manager args passed to every HM configuration.
      hmCommonArgs = {
        inherit
          nix-colors
          pkgsUnstable
          pkgsMaster
          claude-plugins-official
          siderolabs-docs
          gafferPkgs
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

      sharedHmModules = [
        inputs.sops-nix.homeManagerModules.sops
        ./nix/home/modules/google-drive.nix
      ];

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

          modules =
            sharedHmModules
            ++ [
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
          # Per-host nixpkgs/home-manager override; defaults to the shared inputs.
          # The system's `pkgs` comes from `nixosSystem`'s own nixpkgs (base.nix
          # sets allowUnfree; no module pins `nixpkgs.pkgs`).
          # Defaults reference `inputs.*`, NOT the bare names — `nixpkgs ? nixpkgs`
          # would self-reference the formal arg and infinitely recurse.
          nixpkgs ? inputs.nixpkgs,
          home-manager ? inputs.home-manager,
          extraModules ? [ ],
          # Inline home-manager config: if set, HM is activated during NixOS
          # activation (no hm-bootstrap.nix needed). Requires the HM host
          # config module path (e.g., ./nix/home/hosts/nixos-vm.nix).
          inlineHomeManager ? null,
          # Whether to include home-manager at all. Bootstrap images set this
          # false to keep the closure tiny — the real host config takes over
          # after first `nixos-rebuild switch`.
          enableHomeManager ? true,
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
            # These project packages are passed only to hosts that opt into them.
            # Lazy: hosts that do not consume a package never force its build/fetch.
            inherit (ducktapePkgs) bb bbr llama-cpp-openvino;
          };
          modules = [
            ./nix/nixos/modules/base.nix
            ./nix/nixos/hosts/${hostname}
          ]
          ++ nixpkgs.lib.optionals enableHomeManager [
            home-manager.nixosModules.home-manager
            (
              if inlineHomeManager != null then
                {
                  home-manager.useGlobalPkgs = true;
                  home-manager.useUserPackages = true;
                  home-manager.extraSpecialArgs = hmExtraSpecialArgs;
                  home-manager.sharedModules = sharedHmModules;
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
      ducktapePkgs = import ./nix/packages {
        inherit
          lib
          pkgs
          artifacts
          ;
      };
      # gaffer-private's drivectl/drivefs, fetched purely as store paths from
      # cache.allegedly.works/gaffer (no source eval). Empty until gaffer CI's
      # first push populates ./nix/gaffer-pins.json.
      gafferPkgs = import ./nix/packages/gaffer.nix { };
      devTools = import ./nix/flake/devtools.nix {
        inherit
          pkgs
          pkgsUnstable
          ducktapePkgs
          ruffLatest
          ;
      };
      inherit (devTools)
        localOnlyPackages
        systemLibs
        devToolPackages
        ;
    in
    {
      # CI push targets for nix-attic-push, split by destination cache. Under
      # legacyPackages so `nix flake {show,check}` skip them (they force-eval all
      # host closures). drivefs isolation for `main` lives in the imported file.
      legacyPackages.${system} =
        let
          atticTargets = import ./devinfra/ci/nix_attic_targets.nix {
            inherit
              self
              lib
              system
              ducktapePkgs
              ;
          };
        in
        {
          ci-attic-main = atticTargets.main;
          ci-attic-public = atticTargets.public;
        };

      # Development shell — enter via `nix develop` or direnv (`use flake`).
      devShells.${system}.default = pkgs.mkShell {
        # Keep each Python CLI's dependencies in its own wrapper, not the shared shell.
        dontAddPythonPath = "1";
        packages = devToolPackages ++ localOnlyPackages ++ systemLibs.packages;
        inherit (systemLibs) buildInputs;
        LD_LIBRARY_PATH = systemLibs.libraryPath;
      };

      # One binding, because `system` is dynamic: Nix merges repeated *static* attribute paths but
      # rejects a repeated dynamic one, so a second `checks.${system}.…` is an eval error.
      checks.${system} = {
        claude-code-permissions = import ./nix/home/tests/claude-code-permissions.nix {
          inherit pkgs;
        };

        codex-execpolicy-evaluation = import ./nix/home/tests/codex-execpolicy-evaluation.nix {
          inherit pkgs;
          inherit (pkgsMaster) codex;
        };

        gemini-cli-integration = import ./nix/home/tests/gemini-cli-integration.nix {
          inherit pkgs;
        };
      };
      packages.${system} =
        (import ./nix/flake/packages.nix {
          inherit
            self
            system
            pkgs
            ducktapePkgs
            gafferPkgs
            home-manager
            pkgsUnstable
            pkgsMaster
            nix-openclaw
            ruffLatest
            localOnlyPackages
            devToolPackages
            ;
        })
        // {
          # Keep the NixOS prototype under x/; this lazy value does not make
          # unrelated packages depend on the experiment.
          buildbuddy-remote-runner-nixos-image = buildbuddyRemoteRunnerNixosOutputs.packages.${system}.buildbuddy-remote-runner-nixos-image;
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

        # Claude Code web session — headless standalone profile installed by
        # web_setup.sh's home-manager mode. Independent of the shared host
        # structure: it only needs the devtools list and the skills args.
        # Portable across the web container's user (home.username/homeDirectory
        # read from the env), so it must be built/activated with --impure:
        #   home-manager switch --impure --flake .#claude-web
        claude-web = home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            ./nix/home/hosts/claude-web.nix
            {
              _module.args = {
                webDevTools = devToolPackages;
                inherit (hmCommonArgs) sharedSkillsArgs;
              };
            }
          ];
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

        # agent-box - headless CLI-only KubeVirt VM hosting agent users, each under
        # its own scoped identity. `codex` runs OpenAI Codex. See
        # cluster/k8s/parked/agent-box/README.md.
        agent-box = mkNixos {
          hostname = "agent-box";
          username = "codex";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          inlineHomeManager = {
            enableGui = false;
            isK8sWorker = false;
            module = ./nix/home/hosts/agent-box/codex.nix;
          };
        };

        # public-coder-devbox - isolated NixOS build/test VM for the public-coder
        # OpenClaw agent. It is intentionally separate from agent-box so the
        # agent's toolchain and egress policy can evolve independently.
        public-coder-devbox = mkNixos {
          hostname = "public-coder-devbox";
          username = "coder";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          inlineHomeManager = {
            enableGui = false;
            isK8sWorker = false;
            module = ./nix/home/hosts/public-coder-devbox.nix;
          };
        };

        # Gecko - headless CLI-only KubeVirt VM for Claude Code / Codex
        gecko = mkNixos {
          hostname = "gecko";
          username = "agentydragon";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          inlineHomeManager = {
            enableGui = false;
            isK8sWorker = false;
            module = ./nix/home/hosts/gecko.nix;
          };
        };

        # Generic bootstrap NixOS — minimal SSH-able image for initial provisioning.
        bootstrap = mkNixos {
          hostname = "bootstrap";
          username = "agentydragon";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          enableHomeManager = false;
        };

        # Minimal KubeVirt VM that owns the USB CPAP WiFi adapter passed through
        # from the OptiPlex host.
        cpap-gateway = mkNixos {
          hostname = "cpap-gateway";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          enableHomeManager = false;
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

        # Experimental NixOS implementation lives under the BuildBuddy Remote Runner.
        buildbuddy-remote-runner = buildbuddyRemoteRunnerNixosOutputs.nixosConfigurations.buildbuddy-remote-runner;

        # Haku Managed Agents self-hosted worker (Runtime B). fastmcp is a
        # ducktape package, passed in rather than re-derived. The poll loop is
        # worker.py on the anthropic Python SDK now, not `ant` (the anthropic-cli
        # package stays available in the devshell); see nixos.nix.
        haku-managed-agent = nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs = { inherit (ducktapePkgs) fastmcp; };
          modules = [
            ./haku/runtime/managed_agent/self_hosted/nixos.nix
          ];
        };
      };

      # Phone (Android via nix-on-droid). aarch64-linux; see nix/droid/README.md.
      nixOnDroidConfigurations = {
        pixel6 = inputs.nix-on-droid.lib.nixOnDroidConfiguration {
          pkgs = pkgsAarch64;
          modules = [ ./nix/droid/hosts/pixel6.nix ];
        };
      };
    };
}
