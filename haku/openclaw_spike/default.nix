# Nix-built OCI image for Haku's isolated OpenClaw + Claude Code spike.
#
# The gateway package itself is shared with the public-coder image; the source
# pin, npm-wrapper splice, and dist repairs live in openclaw/gateway.nix. This
# file adds the spike's own proxy preload and command-line tooling -- everything
# a Nix package in one reviewable closure, no second Node.
#
# Not an upstream Docker base (the earlier approach): it shipped a second Node
# and was the source of the original runtime compatibility failure. Building the
# image ourselves keeps one Node in one Nix closure.
{
  pkgs,
  nix-openclaw,
}:

let
  openclawGateway = import ../../openclaw/gateway.nix { inherit pkgs nix-openclaw; };
  inherit (openclawGateway) gateway;

  # The same Node 22 runtime as public-coder, and the one used by the gateway
  # build, so there is one Node in the image, not two.
  nodejs = openclawGateway.ocPkgs.nodejs_22;

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
      gawk
      gcc
      git
      gnugrep
      gnumake
      gnused
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

  # The preload imports the same pinned `undici` dependency as the gateway.
  # The shared nested npm lock places it under node_modules/openclaw, which the
  # Nix install copies into the gateway root. Reuse that tree instead of fetching
  # a second, potentially divergent undici package for the preload.
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
