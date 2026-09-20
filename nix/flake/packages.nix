{
  self,
  system,
  pkgs,
  ducktapePkgs,
  gafferPkgs,
  home-manager,
  pkgsUnstable,
  pkgsMaster,
  nix-openclaw,
  ruffLatest,
  localOnlyPackages,
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
  # Installable package for `nix profile install .#devtools` (used by web_setup.sh).
  # Default: Rust claude-hook plus Python statusline.
  devtools = pkgs.symlinkJoin {
    name = "ducktape-devtools";
    paths = devToolPackages ++ localOnlyPackages;
  };
  # Lean devtools for the BuildBuddy Remote Runner image (no rustfmt, ansible).
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
  # Codex pod image (plain Docker, no NixOS/systemd). Tool set is a
  # buildEnv; see cluster/k8s/agents/codex-pod/README.md.
  # Build: nix build .#codex-pod-image
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
  # Ephemeral KubeVirt containerDisk for public-coder-devbox (same shape as
  # cpap-gateway-container-disk above): the VM boots straight into the real
  # build/test configuration and owns its first-boot disk setup through NixOS
  # systemd units rather than cloud-init -- which is what makes it a clean fit
  # for containerDisk's "every boot is a fresh disk" model. No persistent state
  # (Nix store, Bazel/BuildBuddy caches, checkouts) survives an image update or
  # VM restart; that's an accepted tradeoff for auto-published, always-current
  # tooling rather than a manual publish+URL-bump step.
  public-coder-devbox-container-disk =
    let
      diskImage = self.nixosConfigurations.public-coder-devbox.config.system.build.images.qemu-efi;
      diskRoot = pkgs.runCommand "public-coder-devbox-container-disk-root" { } ''
        mkdir -p $out/disk
        cp ${diskImage}/*.qcow2 $out/disk/disk.qcow2
      '';
    in
    pkgs.dockerTools.buildLayeredImage {
      name = "git.allegedly.works/ducktape-ci/public-coder-devbox";
      contents = [ diskRoot ];
      includeStorePaths = false;
      maxLayers = 2;
      extraCommands = ''
        cp --dereference disk/disk.qcow2 disk/disk.qcow2.real
        rm disk/disk.qcow2
        mv disk/disk.qcow2.real disk/disk.qcow2
      '';
      fakeRootCommands = ''
        chown 107:107 disk/disk.qcow2
      '';
      uid = 107;
      gid = 107;
      config = { };
    };
}
