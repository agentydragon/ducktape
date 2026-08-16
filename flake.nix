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
      artifacts =
        let
          data = builtins.fromJSON (builtins.readFile ./nix/artifact-pins.json);
          rawOverrides = builtins.getEnv "DUCKTAPE_ARTIFACT_OVERRIDES";
          overrides = if rawOverrides == "" then { } else builtins.fromJSON rawOverrides;
        in
        builtins.mapAttrs (
          name: spec:
          if overrides ? ${name} then
            # Preserve the URL's basename so consumers that read the store
            # path's suffix (aiquota's buildPythonApplication glob for *.whl,
            # extension-zip unzip) work identically to the fetchurl path.
            # renameWheel-based mkWheel callers are agnostic to this name.
            builtins.path {
              path = /. + overrides.${name};
              name = baseNameOf spec.url;
            }
          else
            pkgs.fetchurl {
              inherit (spec) url;
              hash = "sha256-${spec.sha256}";
            }
        ) data.pins;

      # Each skill ships as its own `skill-<name>` release artifact. Assemble the
      # per-skill `.skill` zips (each already rooted under `<name>/`) into one flat
      # directory of `<name>/` subdirs. Registry: skills/skills_registry.json.
      skillsUnpacked =
        let
          inherit (builtins.fromJSON (builtins.readFile ./skills/skills_registry.json)) skills;
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
            # hostexecd is a fetched release artifact, passed to the module that
            # needs it (only wyrm2/rugged import nix/nixos/modules/hostexecd.nix).
            # Lazy: hosts that don't import the module never force the fetch.
            inherit (ducktapePkgs) hostexecd;
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
        # Anthropic CLI (`ant`): Claude API / Managed Agents control plane, for
        # running `ant beta:*` (haku/runtime/managed_agent/self_hosted). Not needed on RBE.
        ducktapePkgs.anthropic-cli
        pkgs.rustfmt # 1GB (pulls full rustc via RPATH)
        pkgs.ansible # 650MB
        # llvm-addr2line: drop-in for GNU addr2line used by `perf report` for
        # inline-frame symbolization. 10-50x faster on Rust DWARF and keeps a
        # persistent symbol cache across queries from the same process; the
        # debundle perf-profile wrapper (devinfra/js/debundle/pipeline.bzl)
        # prepends a shim that aliases addr2line -> llvm-addr2line when it is
        # on PATH.
        pkgs.llvmPackages.bintools-unwrapped
        # Cluster/infra CLIs (formerly cluster/shell.nix). Used for Talos,
        # Route 53, Nebula PKI, policy validation, and bare-metal provisioning.
        pkgs.talosctl
        pkgs.awscli2 # AWS CLI for Route 53 management
        pkgs.hcloud # Price-comparison helper only; cluster bootstrap does not consume HCloud creds
        pkgs.kyverno # Policy engine CLI (validate manifests, test policies)
        pkgs.nebula # Nebula mesh overlay (nebula-cert for PKI management)
        pkgs.ovhcloud-cli # OVH API CLI (Kimsufi server inventory, boot, IPMI)
        pkgs.python313Packages.ovh # OVH Python client for ad-hoc API scripts
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
        pkgs.nixfmt
        pkgs.statix
        pkgs.ruff
        pkgs.shfmt
        pkgs.buildifier
        pkgs.gofumpt
        pkgs.markdownlint-cli2
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
        pkgs.checkov # Terraform security scanner; backs the checkov_diff pre-commit hook
        pkgs.sops
        pkgs.ssh-to-age
        ducktapePkgs.kubernetes-mcp-server
        ducktapePkgs.bazel-diff
      ];
      # Rust claude-hook is the active hook/shim implementation. The statusline
      # remains Python, exposed through a package that does not put the legacy
      # Python `claude-hook` on PATH.
      devToolPackages = devToolsCommon ++ [
        ducktapePkgs.claude-hook-rs
        ducktapePkgs.claude-statusline
      ];
      # Compatibility alias for older setup scripts selecting #devtools-rust.
      devToolPackagesRust = devToolPackages;
    in
    {
      # CI push targets for nix-attic-push, split by destination cache. Under
      # legacyPackages so `nix flake {show,check}` skip them (they force-eval all
      # host closures). drivefs isolation for `main` lives in the imported file.
      legacyPackages.${system} =
        let
          atticTargets = import ./devinfra/ci/nix_attic_targets.nix { inherit self lib system; };
        in
        {
          ci-attic-main = atticTargets.main;
          ci-attic-public = atticTargets.public;
        };

      # Development shell — enter via `nix develop` or direnv (`use flake`).
      devShells.${system}.default = pkgs.mkShell {
        packages = devToolPackages ++ localOnlyPackages ++ systemLibs.packages;
        inherit (systemLibs) buildInputs;
        LD_LIBRARY_PATH = systemLibs.libraryPath;
      };

      checks.${system}.codex-execpolicy-evaluation =
        import ./nix/home/tests/codex-execpolicy-evaluation.nix
          {
            inherit pkgs;
            inherit (pkgsMaster) codex;
          };

      packages.${system} =
        ducktapePkgs
        // gafferPkgs
        // {
          # Minimal CI package: just bb + sops (no claude-hooks wheel needed).
          citools = pkgs.symlinkJoin {
            name = "ducktape-citools";
            paths = [
              ducktapePkgs.bb
              ducktapePkgs.bbapi
              pkgs.sops
            ];
          };
          # Installable package for `nix profile install .#devtools` (used by web_setup.sh).
          # Default: Rust claude-hook plus Python statusline.
          devtools = pkgs.symlinkJoin {
            name = "ducktape-devtools";
            paths = devToolPackages ++ localOnlyPackages;
          };
          # Compatibility alias for old `web_setup.sh --impl=rust` installs.
          devtools-rust = pkgs.symlinkJoin {
            name = "ducktape-devtools-rust";
            paths = devToolPackagesRust ++ localOnlyPackages;
          };
          # Lean devtools for RBE worker image (no rustfmt, ansible).
          rbetools = pkgs.symlinkJoin {
            name = "ducktape-rbetools";
            paths = devToolPackages;
          };
          # Haku's agent closure: the single shared `.#devtools` plus agent
          # CLIs: fastmcp (`call`/`list --auth <bearer>`) for haku-console and
          # other MCP servers, himalaya for Haku's own mailbox, and tea for
          # Gitea/Forgejo issue/PR/release workflows. This is NOT a devtools
          # fork — it composes the one `.#devtools` and adds agent tools on
          # top. Installed by web_setup.sh when
          # DUCKTAPE_WEB_SETUP_OUTPUT=agent-haku (set in
          # haku/runtime/claude_web_env/setup.sh).
          agent-haku = pkgs.symlinkJoin {
            name = "ducktape-agent-haku";
            paths = [
              self.packages.${system}.devtools
              ducktapePkgs.fastmcp
              # nixpkgs ships himalaya WITHOUT the `oauth2` cargo feature, so any
              # config with backend.auth.type = "oauth2" fails to parse ("missing
              # `oauth2` cargo feature") — OAUTHBEARER against haku-mailbox needs
              # the feature compiled in (verified against the live server).
              (pkgs.himalaya.overrideAttrs (o: {
                cargoBuildFeatures = (o.cargoBuildFeatures or [ ]) ++ [ "oauth2" ];
              }))
              pkgs.tea
            ];
          };
          # home-manager CLI, pinned to our flake input (release-26.05). Used by
          # web_setup.sh's home-manager install mode so activation does not pull
          # an unpinned home-manager from the registry:
          #   nix run .#home-manager -- switch --impure --flake .#claude-web
          inherit (home-manager.packages.${system}) home-manager;
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
          # Haku sandbox image (plain Docker, no NixOS/systemd) — the Nix
          # replacement for cluster/k8s/haku/workspaces/image/Dockerfile. Builds
          # in CI but is NOT yet what the SandboxTemplate pulls; cutover is gated
          # on the runtime checklist in that directory's README.
          # Build: nix build .#haku-sandbox-image
          # Load:  docker load < result
          haku-sandbox-image = import ./cluster/k8s/haku/workspaces/image { inherit pkgs; };
          # Codex pod image (plain Docker, no NixOS/systemd). Tool set is a
          # buildEnv; see cluster/k8s/agents/codex-pod/README.md.
          # Build: nix build .#codex-pod-image
          # Load:  docker load < result
          codex-pod-image = import ./x/codex_pod_image {
            inherit
              pkgs
              pkgsUnstable
              pkgsMaster
              home-manager
              ;
          };
          # Public coder OpenClaw image (plain Docker archive, no Debian base).
          # The gateway and its Node dependency closure come from nix-openclaw;
          # openclaw/default.nix adds the proxy preload and command-line tools.
          # Build: nix build .#openclaw-image
          # Load:  docker load < result
          openclaw-image = import ./openclaw {
            inherit
              nix-openclaw
              pkgs
              ;
          };
          # NixOS-based RBE worker (systemd, envfs, nix-ld).
          # Build: nix build .#nix-rbe-nixos
          # Load:  docker import result/tarball/*.tar.xz nix-rbe-nixos
          nix-rbe-nixos = self.nixosConfigurations.nix-rbe-worker.config.system.build.tarball;
          # Full-NixOS container image for the Haku Managed Agents self-hosted
          # worker (Runtime B, haku/runtime/managed_agent/self_hosted).
          # Build: nix build .#haku-managed-agent-image
          # Load:  docker import result/tarball/*.tar haku-managed-agent
          # Emit an UNCOMPRESSED rootfs tar: the CI step `podman import`s it and
          # compresses the layer once (gzip). The default `pixz -t` xz pass would
          # just be decompressed and re-gzipped — wasted work — and importing the
          # `.tar.xz` directly yields an inconsistent layer the node rejects with
          # "wrong diff id calculated on extraction".
          haku-managed-agent-image =
            self.nixosConfigurations.haku-managed-agent.config.system.build.tarball.override
              {
                compressCommand = "cat";
                compressionExtension = "";
                extraInputs = [ ];
                # The agent toolset's `bash` tool execs `/bin/bash` at that literal
                # path (PATH-independent). NixOS activation would create it, but we
                # run the closure directly without booting, so bake /bin/{bash,sh}
                # into the rootfs here (-> the system-path bash at the stable /sw).
                # extraCommands REPLACES the docker-container profile's value (and
                # must be an executable script, not a string), so the profile's /etc
                # + /proc/sys/dev fixups are re-applied here too.
                extraCommands = self.nixosConfigurations.haku-managed-agent.pkgs.writeScript "haku-managed-agent-tarball-extra" ''
                  rm etc
                  mkdir -p proc sys dev etc bin
                  chmod u+w bin
                  ln -sf /sw/bin/bash bin/bash
                  ln -sf /sw/bin/sh bin/sh
                '';
              };
          # Pre-built UEFI qcow2 VM images for Proxmox deployment.
          # Build: nix build .#wyrm2-image
          # Uses built-in system.build.images.qemu-efi (nixos-generators upstreamed in 25.05+).
          wyrm2-image = self.nixosConfigurations.wyrm2.config.system.build.images.qemu-efi;
          bootstrap-image = self.nixosConfigurations.bootstrap.config.system.build.images.qemu-efi;
          # Full public-coder-devbox image: the VM boots straight into the real
          # build/test configuration and owns its first-boot disk setup through
          # NixOS systemd units rather than cloud-init.
          public-coder-devbox-image =
            self.nixosConfigurations.public-coder-devbox.config.system.build.images.qemu-efi;
          # Full agent-box host image: the VM boots straight into the real config
          # (codex user, Codex CLI, planted keys) — no bootstrap + nixos-rebuild
          # switch. Published by vm-images-publisher with IMAGE_OUTPUT=agent-box-image,
          # OBJECT_PREFIX=agent-box. cloud-init still injects the persisted host key.
          agent-box-image = self.nixosConfigurations.agent-box.config.system.build.images.qemu-efi;
          # Stateless KubeVirt containerDisk: the qcow2 lives at /disk/disk.qcow2
          # in an OCI image, owned by KubeVirt's qemu UID (107). Flux publishes
          # a new tag when this output changes and updates the VM manifest.
          cpap-gateway-container-disk =
            let
              diskImage = self.nixosConfigurations.cpap-gateway.config.system.build.images.qemu-efi;
              diskRoot = pkgs.runCommand "cpap-gateway-container-disk-root" { } ''
                mkdir -p $out/disk
                cp ${diskImage}/*.qcow2 $out/disk/disk.qcow2
              '';
            in
            pkgs.dockerTools.buildLayeredImage {
              name = "ghcr.io/agentydragon/cpap-gateway";
              contents = [ diskRoot ];
              includeStorePaths = false;
              maxLayers = 2;
              # streamLayeredImage assembles contents through symlinkJoin;
              # dereference the build-time link before the layer is tarred.
              extraCommands = ''
                cp --dereference disk/disk.qcow2 disk/disk.qcow2.real
                rm disk/disk.qcow2
                mv disk/disk.qcow2.real disk/disk.qcow2
              '';
              fakeRootCommands = ''
                chown 107:107 disk/disk.qcow2
              '';
              # KubeVirt's virt-launcher runs qemu as UID 107. dockerTools
              # applies this ownership to the layer without chowning the Nix
              # store output itself.
              uid = 107;
              gid = 107;
              config = { };
            };
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
        # cluster/k8s/agent-box/README.md.
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

        # NixOS-based RBE worker with full Bazel compat (envfs, nix-ld).
        nix-rbe-worker = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            ./x/nix_rbe_image/nixos.nix
          ];
        };

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
    };
}
