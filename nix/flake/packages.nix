{
  self,
  system,
  pkgs,
  artifacts,
  ducktapePkgs,
  gafferPkgs,
  home-manager,
  pkgsUnstable,
  pkgsMaster,
  nix-openclaw,
  ruffLatest,
  localOnlyPackages,
  buildBuddyRunnerTools,
  preCommitPackages,
  devToolPackages,
}:

ducktapePkgs
// gafferPkgs
// {
  # Minimal CI package: the BuildBuddy CLIs + sops (no Claude statusline wheel needed).
  citools = pkgs.symlinkJoin {
    name = "ducktape-citools";
    paths = [
      ducktapePkgs.bb
      ducktapePkgs.bbapi
      ducktapePkgs.bbr
      pkgs.sops
    ];
  };
  # Focused PATH for running .pre-commit-config.yaml hooks via `nix shell` or CI.
  precommit = pkgs.symlinkJoin {
    name = "ducktape-precommit";
    paths = preCommitPackages;
  };
  # Installable package for `nix profile install .#devtools` (used by web_setup.sh).
  # Default: Rust claude-hook plus Python statusline.
  devtools = pkgs.symlinkJoin {
    name = "ducktape-devtools";
    paths = devToolPackages ++ localOnlyPackages;
  };
  # BuildBuddy runner VM tools: Bazel via Bazelisk, bazel-diff, and bb for
  # best-effort CI diagnostics. Pre-commit runs in a separate GitHub workflow.
  buildbuddy-remote-runner-tools = pkgs.symlinkJoin {
    name = "ducktape-buildbuddy-remote-runner-tools";
    paths = buildBuddyRunnerTools;
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
  # Experimental Nix-based RBE container image (plain Docker, no NixOS/systemd).
  # Build: nix build .#nix-rbe-container-image
  # Load:  docker load < result
  nix-rbe-container-image = import ../../devinfra/rbe_container_image/x/nix { inherit pkgs; };
  # Haku sandbox image (plain Docker, no NixOS/systemd) — the Nix
  # replacement for cluster/k8s/haku/workspaces/image/Dockerfile. Builds
  # in CI but is NOT yet what the SandboxTemplate pulls; cutover is gated
  # on the runtime checklist in that directory's README.
  # Build: nix build .#haku-sandbox-image
  # Load:  docker load < result
  haku-sandbox-image = import ../../cluster/k8s/haku/workspaces/image { inherit pkgs; };
  # agentplane's sandbox image: a box's command-line tools, as one list
  # (agentplane/images/sandbox.nix).
  # Build: nix build .#agentplane-sandbox-image
  # Load:  docker load < result
  agentplane-sandbox-image = import ../../agentplane/images/sandbox.nix { inherit pkgs; };
  # The build box's image: the sandbox image plus Bazel and its toolchain (agentplane/images/build.nix).
  # Build: nix build .#agentplane-sandbox-build-image
  # Load:  docker load < result
  agentplane-sandbox-build-image = import ../../agentplane/images/build.nix { inherit pkgs; };
  # agentplane's runner image: the sandbox image plus the released runner wheel and nixpkgs'
  # Claude Code and Codex (agentplane/runner/image.nix).
  # Build: nix build .#agentplane-runner-image
  # Load:  docker load < result
  agentplane-runner-image = import ../../agentplane/runner/image.nix {
    inherit pkgs pkgsUnstable;
    wheel = artifacts.agentplane-runner;
  };
  # Parked Codex pod image experiment (plain Docker, no NixOS/systemd). Flake
  # output retained; see x/codex_pod_image/deploy/README.md for status.
  # Build: nix build .#codex-pod-image (currently fails at evaluation; see deployment README)
  # Load:  docker load < result
  codex-pod-image = import ../../x/codex_pod_image {
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
  openclaw-image = import ../../openclaw {
    inherit
      ducktapePkgs
      nix-openclaw
      pkgs
      ruffLatest
      ;
  };
  # Haku's Claude-backed OpenClaw spike. Same Nix build mechanism as
  # openclaw-image; the gateway is nix-openclaw's npm-package build pinned
  # to the 2026.8.1 beta (nix-openclaw only tracks stable) -- see the file.
  # Build: nix build .#haku-openclaw-spike-image
  # Load:  docker load < result
  haku-openclaw-spike-image = import ../../haku/openclaw_spike {
    inherit nix-openclaw pkgs ruffLatest;
  };
  # Parked full-NixOS container image for the Haku Managed Agents self-hosted
  # worker (Runtime B). Kept as a manual build output with its recipe beside
  # the component; automatic build/publish and Attic caching are disabled.
  # Build: nix build .#haku-managed-agent-image
  # Load:  docker import result/tarball/*.tar haku-managed-agent
  haku-managed-agent-image = import ../../haku/runtime/managed_agent/self_hosted/image.nix {
    inherit self;
  };
  # Pre-built UEFI qcow2 VM images for Proxmox deployment.
  # Build: nix build .#wyrm2-image
  # Uses built-in system.build.images.qemu-efi (nixos-generators upstreamed in 25.05+).
  wyrm2-image = self.nixosConfigurations.wyrm2.config.system.build.images.qemu-efi;
  bootstrap-image = self.nixosConfigurations.bootstrap.config.system.build.images.qemu-efi;
  # Full agent-box host image: the VM boots straight into the real config
  # (codex user, Codex CLI, planted keys) — no bootstrap + nixos-rebuild
  # switch. Published by vm-images-publisher with IMAGE_OUTPUT=agent-box-image,
  # OBJECT_PREFIX=agent-box. cloud-init still injects the persisted host key.
  agent-box-image = self.nixosConfigurations.agent-box.config.system.build.images.qemu-efi;
  # Stateless KubeVirt containerDisk for the CPAP gateway VM.
  cpap-gateway-container-disk = import ../../cpap/gateway/container-disk.nix {
    inherit pkgs self;
  };
  # Ephemeral KubeVirt containerDisk for public-coder-devbox. The VM boots into
  # its build/test configuration and handles fresh-disk setup through NixOS
  # systemd units.
  public-coder-devbox-container-disk =
    import ../../openclaw/public_coder_agent/devbox/container-disk.nix
      {
        inherit pkgs self;
      };
}
