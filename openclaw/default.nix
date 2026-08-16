{ pkgs, nix-openclaw }:

let
  system = pkgs.stdenv.hostPlatform.system;
  gateway = nix-openclaw.packages.${system}.openclaw-gateway;

  # Keep the shell/coreutils surface needed by public-coder-agent's init
  # container, plus the tools the agent uses for repository and API work. No
  # SSH client is included: public-coder uses HTTPS Git through the proxy.
  tools = with pkgs; [
    bashInteractive
    busybox
    cacert
    coreutils
    curl
    git
    jq
    kubectl
    nodejs_22
    python3
    ripgrep
    tini
  ];

  # The preload imports undici. Put it beside the Nix gateway's node_modules so
  # Node's ESM resolver finds the dependency exactly as it did in /app in the
  # Docker-built image. The symlink keeps the dependency closure shared.
  proxySetup = pkgs.runCommand "openclaw-proxy-setup" { } ''
    mkdir -p "$out/lib/openclaw"
    cp ${./proxy-setup.mjs} "$out/lib/openclaw/proxy-setup.mjs"
    ln -s ${gateway}/lib/openclaw/node_modules "$out/lib/openclaw/node_modules"
  '';

  path = pkgs.lib.makeBinPath ([ gateway ] ++ tools);
in
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/agentydragon/openclaw";
  # CI supplies the sortable devel-* tag selected by Flux.
  tag = null;

  contents = [
    gateway
    proxySetup
  ]
  ++ tools;
  maxLayers = 100;

  fakeRootCommands = ''
    mkdir -p home/openclaw tmp etc/ssl/certs
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

    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
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
