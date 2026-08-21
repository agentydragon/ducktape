# Nix-built OCI image for Haku's isolated OpenClaw + Claude Code spike.
#
# The beta npm artifact already contains OpenClaw's built dist and declares its
# runtime dependencies. Use the standard Nix npm flow here; the source build
# is intentionally left to OpenClaw's release pipeline.
{ pkgs }:

let
  openclawNpmTarball = pkgs.fetchurl {
    url = "https://registry.npmjs.org/openclaw/-/openclaw-2026.7.2-beta.7.tgz";
    hash = "sha256-LMIGyJigYsugWM9pwtOuVqC9BSm0Hp06qQ9Cywnr/OQ=";
  };

  # npm publishes no lockfile in the package tarball. Keep the generated lock
  # next to this derivation so buildNpmPackage still gets a fully pinned graph.
  openclawNpmSource = pkgs.runCommand "openclaw-2026.7.2-beta.7-npm-source" { } ''
    mkdir -p "$out"
    tar -xzf ${openclawNpmTarball} --strip-components=1 -C "$out"
    cp ${./openclaw-package-lock.json} "$out/package-lock.json"
  '';

  # nixpkgs-unstable's Node 26 satisfies the beta's engine range and embeds a
  # new enough SQLite for OpenClaw's WAL-reset safety check.
  nodeForOpenClaw = pkgs.nodejs-slim_26;

  gateway = pkgs.buildNpmPackage {
    pname = "openclaw-gateway";
    version = "2026.7.2-beta.7";
    src = openclawNpmSource;
    npmDepsHash = "sha256-hKlux/NDxo458CNQvALdi0jVnnG347zIX0S2x0kYF7U=";
    dontNpmBuild = true;
    npmInstallFlags = [
      "--omit=dev"
      "--ignore-scripts"
      "--legacy-peer-deps"
    ];
    nativeBuildInputs = [ pkgs.makeWrapper ];

    installPhase = ''
      mkdir -p "$out/lib/openclaw" "$out/bin"
      cp -R . "$out/lib/openclaw/"
      makeWrapper ${nodeForOpenClaw}/bin/node "$out/bin/openclaw" \
        --add-flags "$out/lib/openclaw/openclaw.mjs" \
        --set-default OPENCLAW_NIX_MODE "1" \
        --set-default OPENCLAW_DISABLE_PERSISTED_PLUGIN_REGISTRY "1"
    '';

    dontFixup = true;
    dontStrip = true;
    dontPatchShebangs = true;
  };

  proxySetup = pkgs.runCommand "haku-openclaw-proxy-setup" { } ''
    mkdir -p "$out/lib/openclaw"
    cp ${../../openclaw/proxy-setup.mjs} "$out/lib/openclaw/proxy-setup.mjs"
    ln -s ${gateway}/lib/openclaw/node_modules "$out/lib/openclaw/node_modules"
  '';

  # The old Dockerfile installed Bazelisk as `/usr/local/bin/bazel`; retain the
  # command name haku-state tooling uses while keeping the upstream Bazelisk
  # package and its version-selection behavior.
  bazel = pkgs.writeShellScriptBin "bazel" ''
    exec ${pkgs.bazelisk}/bin/bazelisk "$@"
  '';

  # Mirrors the old Dockerfile's runtime surface, but each tool is now pinned
  # by flake.lock / nixpkgs rather than an apt repository or an ad-hoc curl
  # download. Claude Code is the Nix package, so it also avoids an
  # image-build-time npm install.
  tools = with pkgs; [
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
    nodeForOpenClaw
    openssl
    procps
    python3
    ripgrep
    ruff
    tea
  ];

  toolPath = pkgs.lib.makeBinPath ([ gateway ] ++ tools);
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

  # Keep the old Dockerfile's /app preload path for the deployment contract,
  # while the actual imported module lives beside the packaged node_modules.
  fakeRootCommands = ''
    mkdir -p app home/openclaw tmp usr/bin
    chmod 1777 tmp
    cp ${../../openclaw/proxy-setup.mjs} app/proxy-setup.mjs
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/app";
    Env = [
      "PATH=${toolPath}:/home/node/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      "HOME=/home/openclaw"
      "USER=openclaw"
      "NODE_ENV=production"
      "NODE_OPTIONS=--import=file://${proxySetup}/lib/openclaw/proxy-setup.mjs"
      "NPM_CONFIG_PREFIX=/home/openclaw/.local"
      "NPM_CONFIG_CACHE=/home/openclaw/.cache/npm"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
    ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
    Cmd = [
      "${gateway}/bin/openclaw"
      "gateway"
    ];
  };
}
