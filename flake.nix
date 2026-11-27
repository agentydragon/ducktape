{
  description = "agentydragon's NixOS, home-manager, and development configurations";

  inputs = {
    # The devshell flake (nix/flake.nix) pins nixpkgs, nixpkgs-unstable and
    # fenix in nix/flake.lock; direnv loads it as `path:./nix` so a reload never
    # hashes or copies the whole repo. After bumping them there, run
    # `nix flake update devshell` here; checks.devshell-lock-sync enforces it.
    devshell.url = "path:./nix";
    nixpkgs.follows = "devshell/nixpkgs";
    nixpkgs-unstable.follows = "devshell/nixpkgs-unstable";
    fenix.follows = "devshell/fenix";

    nix-fast-build = {
      url = "github:Mic92/nix-fast-build/1.6.0";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # OpenClaw's Nix packaging. The image output below consumes its pinned
    # gateway derivation and adds the small public-coder runtime toolset.
    nix-openclaw = {
      url = "github:openclaw/nix-openclaw";
      inputs.nixpkgs.follows = "nixpkgs-unstable";
    };

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
      fenix,
      home-manager,
      nix-colors,
      nixGL,
      nix-openclaw,
      nix-fast-build,
      claude-plugins-official,
      siderolabs-docs,
      ...
    }@inputs:
    let
      system = "x86_64-linux";
      packageSets = import ./nix/flake/package_sets.nix {
        inherit
          nixpkgs
          nixpkgs-unstable
          fenix
          system
          ;
      };
      inherit (packageSets)
        codexAwsLcOverlay
        pkgs
        pkgsUnstable
        artifacts
        ruffLatest
        ducktapePkgs
        devTools
        ;
      # Keep the experimental BuildBuddy Remote Runner NixOS configuration and
      # output definitions with the experiment; the root flake only registers them.
      buildbuddyRemoteRunnerNixosOutputs =
        import ./devinfra/buildbuddy/images/runner/x/nixos/flake-outputs.nix
          {
            inherit nixpkgs self system;
          };
      # Each skill ships as its own `skill-<name>` release artifact. Assemble the
      # per-skill `.skill` zips (each already rooted under `<name>/`) into one flat
      # directory of `<name>/` subdirs. Registry: skills/skills_registry.json.
      skillsUnpacked =
        let
          registry = builtins.fromJSON (builtins.readFile ./skills/skills_registry.json);
          skills = builtins.filter (
            skill: (skill.nix or true) && builtins.hasAttr "skill-${skill.name}" artifacts
          ) registry.skills;
        in
        pkgs.runCommand "skills" { nativeBuildInputs = [ pkgs.libarchive ]; } (
          "mkdir $out\n"
          + pkgs.lib.concatMapStringsSep "\n" (s: "bsdtar -xf ${artifacts."skill-${s.name}"} -C $out") skills
        );

      pkgsMaster = import nixpkgs-master {
        inherit system;
        config.allowUnfree = true;
        overlays = [ codexAwsLcOverlay ];
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
          # Most host modules are kept under nix/nixos/hosts. Component-owned
          # guests can keep their module beside the component's other recipes.
          hostModule ? ./nix/nixos/hosts/${hostname},
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
          # Component guests can opt into package groups from the root flake.
          extraSpecialArgs ? { },
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
            inherit pkgsUnstable;
            # These project packages are passed only to hosts that opt into them.
            # Lazy: hosts that do not consume a package never force its build/fetch.
            inherit (ducktapePkgs)
              bb
              bbr
              cpap-gateway-health
              llama-cpp-openvino
              llama-router
              ;
          }
          // extraSpecialArgs;
          modules = [
            ./nix/nixos/modules/base.nix
            hostModule
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
      # gaffer-private's drivectl/drivefs, fetched purely as store paths from
      # cache.allegedly.works/gaffer (no source eval). Empty until gaffer CI's
      # first push populates ./nix/gaffer-pins.json.
      gafferPkgs = import ./nix/packages/gaffer.nix { };
      inherit (devTools)
        localOnlyPackages
        buildBuddyRunnerTools
        preCommitPackages
        devToolPackages
        ;
    in
    {
      # Purpose-specific targets for nix-attic-push, split by destination
      # cache. These include the host closures we intend to publish, so keep
      # them out of ordinary package/check outputs. drivefs isolation for
      # `main` lives in the imported file.
      atticPushTargets.${system} =
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
        atticTargets;

      # Same derivation as the devshell flake's (nix/flake.nix), which direnv loads.
      devShells.${system} = {
        default = packageSets.devShell;
        ai-usage = import ./x/ai_sub_value/shell.nix {
          inherit pkgs;
          baseShell = self.devShells.${system}.default;
        };
      };

      # One binding, because `system` is dynamic: Nix merges repeated *static* attribute paths but
      # rejects a repeated dynamic one, so a second `checks.${system}.…` is an eval error.
      checks.${system} = {
        environment-tools = import ./agentplane/env/images/ducktape_tools_check.nix {
          inherit pkgs;
          tools = self.packages.${system}.environment-tools;
        };

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

        devshell-lock-sync = import ./nix/flake/devshell-lock-sync.nix {
          inherit pkgs;
          rootLock = builtins.fromJSON (builtins.readFile ./flake.lock);
          devshellLock = builtins.fromJSON (builtins.readFile ./nix/flake.lock);
        };
      }
      // import ./nix/flake/artifact-checks.nix {
        inherit pkgs artifacts;
        packages = self.packages.${system};
      };
      packages.${system} =
        (import ./nix/flake/packages.nix {
          nixFastBuild = nix-fast-build;
          inherit
            self
            system
            pkgs
            artifacts
            ducktapePkgs
            gafferPkgs
            home-manager
            pkgsUnstable
            pkgsMaster
            nix-openclaw
            ruffLatest
            localOnlyPackages
            buildBuddyRunnerTools
            preCommitPackages
            devToolPackages
            ;
        })
        // {
          # Keep the NixOS prototype under x/; this lazy value does not make
          # unrelated packages depend on the experiment.
          buildbuddy-remote-runner-nixos-image =
            buildbuddyRemoteRunnerNixosOutputs.packages.${system}.buildbuddy-remote-runner-nixos-image;
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
        # structure: it uses the shared devToolPackages core and skills args.
        # Unlike .#devtools, it intentionally omits localOnlyPackages.
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
        # cluster/parked/agent-box/README.md.
        agent-box = mkNixos {
          hostname = "agent-box";
          username = "codex";
          hostModule = ./cluster/parked/agent-box/nix/nixos.nix;
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          inlineHomeManager = {
            enableGui = false;
            isK8sWorker = false;
            module = ./cluster/parked/agent-box/nix/home/codex.nix;
          };
        };

        # Gecko - headless CLI-only KubeVirt VM for Claude Code / Codex
        gecko = mkNixos {
          hostname = "gecko";
          username = "agentydragon";
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          hostModule = ./cluster/parked/gecko/nix/nixos.nix;
          inlineHomeManager = {
            enableGui = false;
            isK8sWorker = false;
            module = ./cluster/parked/gecko/nix/home.nix;
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
          hostModule = ./cpap/gateway/nixos.nix;
          hardwareModule = ./nix/nixos/modules/vm-hardware.nix;
          enableHomeManager = false;
        };

        # NixOS guest used by the KubeVirt Agentplane execution environment.
        agentplane-environment-vm = mkNixos {
          hostname = "agentplane-environment-vm";
          username = "runner";
          hostModule = ./agentplane/env/images/vm.nix;
          # Keep the guest's repository tooling identical to the Pod runner's.
          extraSpecialArgs = {
            inherit preCommitPackages;
            firecrackerGuest = false;
          };
          enableHomeManager = false;
        };

        # Firecracker guest shares the environment runtime and tools with KubeVirt, using ordered
        # virtio-mmio drives and a host thaw handshake for time and per-environment credentials.
        agentplane-environment-firecracker = mkNixos {
          hostname = "agentplane-environment-firecracker";
          username = "runner";
          hostModule = ./agentplane/env/images/firecracker.nix;
          extraSpecialArgs = {
            inherit preCommitPackages;
            firecrackerGuest = true;
          };
          enableHomeManager = false;
        };

        # Isolated prebuilt-image spike; the deployed Ubuntu KVM pool is unchanged.
        buildbuddy-kvm-worker-spike = mkNixos {
          hostname = "buildbuddy-kvm-worker-spike";
          hostModule = ./devinfra/buildbuddy/kvm/x/nixos_worker/nixos.nix;
          enableHomeManager = false;
        };

        # Minimal NixOS container for testing Bazel compatibility.
        # Not a real host — see devinfra/nixos_bazel_test/nixos.nix.
        bazel-test = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            ./devinfra/nixos_bazel_test/nixos.nix
            home-manager.nixosModules.home-manager
          ];
        };

        # Experimental NixOS implementation lives under the BuildBuddy Remote Runner.
        buildbuddy-remote-runner =
          buildbuddyRemoteRunnerNixosOutputs.nixosConfigurations.buildbuddy-remote-runner;

        # Haku Managed Agents self-hosted worker (Runtime B). fastmcp is a
        # ducktape package, passed in rather than re-derived. The poll loop is
        # worker.py on the anthropic Python SDK now, not `ant` (the anthropic-cli
        # package stays available in the devshell); see nixos.nix.
        haku-managed-agent = nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs = { inherit (ducktapePkgs) fastmcp; };
          modules = [
            ./haku/runtime/x/managed_agent/self_hosted/nixos.nix
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
