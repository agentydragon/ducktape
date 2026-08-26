# Nix-built OCI image for Haku's isolated OpenClaw + Claude Code spike.
#
# Same build mechanism as openclaw/default.nix (public-coder): the gateway and
# its Node dependency closure come from nix-openclaw, and this file adds the
# proxy preload plus the spike's command-line tooling. Everything is a Nix
# package in one reviewable closure -- no second Node, no upstream Docker base.
#
# Deviation from public-coder: that image consumes nix-openclaw's *stable*
# prebuilt gateway (2026.7.1-2). This spike needs the newer 2026.8.1 beta line
# for its Claude model metadata, and nix-openclaw only tracks stable releases,
# so we build the gateway ourselves from a beta `sourceInfo` override (a
# first-class nix-openclaw path -- it ships a `source-override-render` check).
# Bumping the beta = update `betaSourceInfo` below (tag/rev/hashes).
#
# History: this image used to `dockerTools.pullImage` the upstream
# `ghcr.io/openclaw/openclaw` beta and layer Nix tools on top. That hybrid was
# retired 2026-08 because it shipped two Node runtimes (the base image's Node
# linked an unsafe system SQLite 3.51.2 that OpenClaw's WAL-safety guard rejects
# at startup) and because building the gateway ourselves keeps the whole runtime
# in one controlled Nix closure.
{
  pkgs,
  nix-openclaw,
}:

let
  system = pkgs.stdenv.hostPlatform.system;

  # nixpkgs + nix-openclaw's overlay, matching how nix-openclaw builds its own
  # package set, so callPackage resolves the gateway's pnpm/fetch helpers.
  #
  # Plus a Node bump: nix-openclaw hardcodes nodejs_22 (22.23.2 -> SQLite 3.51.2),
  # which the beta's WAL-safety guard rejects at startup. Node 24 (24.19.0 ->
  # SQLite 3.53.3) is WAL-safe and within OpenClaw's engines range. Overriding
  # nodejs_22 flows through the whole gateway build and the PATH Node below.
  # TODO: upstream a configurable/bumped gateway Node to nix-openclaw, drop this.
  ocPkgs = import nix-openclaw.inputs.nixpkgs {
    inherit system;
    overlays = [
      nix-openclaw.overlays.default
      (_final: prev: { nodejs_22 = prev.nodejs_24; })
    ];
  };

  # Beta source override. Mirrors nix/sources/openclaw-source.nix from
  # nix-openclaw but pinned to the 2026.8.1-beta.3 release. Omitting
  # `gatewayNpmDepsHash` selects nix-openclaw's from-source gateway build, which
  # uses `pnpmDepsHash` for its pnpm dependency FOD.
  betaSourceInfo = {
    owner = "openclaw";
    repo = "openclaw";
    pnpmMajor = "11";
    applyPublicSurfaceHardlinksPatch = false;
    applySkipPluginAutoEnableNixModePatch = false;
    # nix-openclaw's nix-store-plugin-ownership patch is written against the
    # stable source and does not apply to this beta -- because the beta already
    # carries the same behavior upstream (discovery.ts trusts IMMUTABLE_NIX_STORE
    # roots; hardlink-policy.ts trusts nix-store roots in nix mode). Disable it;
    # the beta trusts its own nix-store plugins natively.
    applyNixStorePluginOwnershipPatch = false;
    releaseTag = "v2026.8.1-beta.3";
    releaseVersion = "2026.8.1-beta.3";
    runtimePluginVersion = "2026.8.1";
    # a8c1973 is the *annotated tag object* for v2026.8.1-beta.3; fetchFromGitHub
    # resolves it to the tagged commit (5831b807). Pinning the tag object is
    # stable -- annotated tags are immutable unless force-repushed.
    rev = "a8c1973388fa2645fce83f0239b2356744a98045";
    # fetchFromGitHub tree hash for the release rev.
    hash = "sha256-BTrlg8Y88j50hS3EHDCGQhh0k9zSbZt58b2LmYMcq8w=";
    # pnpm dependency FOD hash for this release. Independent of the pnpm 11.15.1
    # override below: nixpkgs' fetcherVersion-4 fetch leaves pnpm's
    # `manage-package-manager-versions` on, so it self-switches to the source's
    # `packageManager` (11.15.1) regardless of the override -- the override only
    # changes the gateway's *offline* install reader (see the splice comment).
    pnpmDepsHash = "sha256-v/iNnAuMAvIsTRJXIvB29iKAH3hb5qExytr0ADQLWLE=";
  };

  # nix-openclaw hardcodes pnpm 11.2.2 (nix/packages/pnpm-11.nix), but the beta's
  # pnpm-lock.yaml is authored by pnpm 11.15.1 (lockfileVersion 9.0). The two
  # stages of the from-source build see different pnpm versions, and that split
  # is the bug:
  #   * The dependency FOD fetch keeps pnpm's `manage-package-manager-versions`
  #     on, so pnpm self-switches to the source's `packageManager` (11.15.1) and
  #     writes the store in 11.15.1's format -- whatever nix-openclaw pins.
  #   * The gateway's offline `pnpm install` runs after gateway-postpatch strips
  #     the `packageManager` field, so it uses nix-openclaw's pinned pnpm (11.2.2)
  #     directly. That 11.2.2 reader cannot resolve a normally-locked dependency
  #     (@clack/core) out of the 11.15.1-written store and aborts with
  #     ERR_PNPM_NO_OFFLINE_TARBALL.
  # Splicing pnpm 11.15.1 into a copy of the nix-openclaw tree makes the offline
  # reader match the store writer. The `--replace-fail`s make an upstream pnpm
  # bump fail loudly here rather than silently reverting the override.
  # TODO: upstream a configurable/bumped gateway pnpm to nix-openclaw, drop this.
  patchedNixOpenclaw = ocPkgs.runCommand "nix-openclaw-pnpm-11.15.1" { } ''
    cp -r ${nix-openclaw} "$out"
    chmod -R u+w "$out"
    substituteInPlace "$out/nix/packages/pnpm-11.nix" \
      --replace-fail 'version = "11.2.2";' 'version = "11.15.1";' \
      --replace-fail \
        'hash = "sha256-mcS+gx7SMYKYlRQtlnk9vnWvxTeVkzrtg2bmjczh4bg=";' \
        'hash = "sha256-J0YGKbEBEWBOf5iIJ1O1M5iYaCDCDgoGXzpKXp59tx8=";'
  '';

  gateway =
    (import "${patchedNixOpenclaw}/nix/packages" {
      pkgs = ocPkgs;
      sourceInfo = betaSourceInfo;
    }).openclaw-gateway;

  # The single Node runtime (Node 24, per the overlay above) on PATH -- the same
  # Node the gateway build uses, so there is one Node in the image, not two.
  nodejs = ocPkgs.nodejs_24;

  # The old Dockerfile installed Bazelisk as `bazel`; keep that command name for
  # the haku-state tooling while using the upstream Bazelisk version selection.
  bazel = pkgs.writeShellScriptBin "bazel" ''
    exec ${pkgs.bazelisk}/bin/bazelisk "$@"
  '';

  # Mirrors the old runtime tool surface, each pinned by flake.lock / nixpkgs.
  # Claude Code is the Nix package (no image-build-time npm install). `nodejs`
  # (the gateway's own Node, appended below) goes on PATH now that there is no
  # upstream base image to provide one.
  tools =
    with pkgs;
    [
      bashInteractive
      bazel
      binutils
      cacert
      claude-code
      coreutils
      curl
      gcc
      git
      gnumake
      jdk_headless
      jq
      kubectl
      less
      openssl
      procps
      python3
      ripgrep
      ruff
      tea
    ]
    ++ [ nodejs ];

  # The preload imports undici. Put it beside the gateway's node_modules so
  # Node's ESM resolver finds the dependency exactly as it did in /app on the
  # old image. The symlink keeps the dependency closure shared.
  proxySetup = pkgs.runCommand "openclaw-spike-proxy-setup" { } ''
    mkdir -p "$out/lib/openclaw"
    cp ${../../openclaw/proxy-setup.mjs} "$out/lib/openclaw/proxy-setup.mjs"
    ln -s ${gateway}/lib/openclaw/node_modules "$out/lib/openclaw/node_modules"
  '';

  path = pkgs.lib.makeBinPath ([ gateway ] ++ tools);
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/haku-openclaw-spike";
  # CI supplies the sortable devel-* tag selected by Flux.
  tag = null;

  contents = [
    gateway
    proxySetup
  ]
  ++ tools;
  maxLayers = 100;

  fakeRootCommands = ''
    mkdir -p home/openclaw tmp etc/ssl/certs usr/bin
    chmod 1777 tmp
    chown -R 1000:1000 home/openclaw

    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/sh
    openclaw:x:1000:1000:OpenClaw:/home/openclaw:/bin/sh
    nobody:x:65534:65534:Nobody:/:/bin/false
    PASSWD
    cat > etc/group <<'GROUP'
    root:x:0:
    openclaw:x:1000:
    nogroup:x:65534:
    GROUP
    cat > etc/nsswitch.conf <<'NSS'
    passwd: files
    group: files
    hosts: files dns
    NSS

    # Default trust store. The k8s deployment mounts the interception CA over
    # this path at runtime (see cluster/k8s/agents/haku-openclaw-spike README).
    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/home/openclaw";
    Env = [
      "PATH=${path}"
      "HOME=/home/openclaw"
      "USER=openclaw"
      "NODE_ENV=production"
      "NODE_OPTIONS=--import=file://${proxySetup}/lib/openclaw/proxy-setup.mjs"
      "NPM_CONFIG_PREFIX=/home/openclaw/.local"
      "NPM_CONFIG_CACHE=/home/openclaw/.cache/npm"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
    ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
    Entrypoint = [
      "${pkgs.tini}/bin/tini"
      "-s"
      "--"
    ];
    Cmd = [
      "${gateway}/bin/openclaw"
      "gateway"
    ];
  };
}
