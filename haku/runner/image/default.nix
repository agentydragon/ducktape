# Nix-built OCI image for the Haku Console harness runner.
# The runner and its toolchain are composed here instead of layering a Debian base
# with separately maintained package archives.
{
  pkgs,
  pkgsUnstable,
  pkgsMaster,
}:
let
  pythonEnv = pkgs.python3.withPackages (
    pythonPackages: with pythonPackages; [
      anyio
      pydantic
      tenacity
      websockets
    ]
  );

  runnerSource = pkgs.runCommand "haku-runner-source" { } ''
    mkdir -p $out/lib
    cp -a ${../../..}/haku $out/lib/
  '';

  runner = pkgs.writeShellScriptBin "haku-runner" ''
    export PYTHONPATH=${runnerSource}/lib
    exec ${pythonEnv}/bin/python -m haku.runner.runner "$@"
  '';

  tools = pkgs.buildEnv {
    name = "haku-harness-runner-tools";
    paths = [
      runner
      pythonEnv
      pkgs.bashInteractive
      pkgs.coreutils
      pkgs.openssl
      pkgs.curl
      pkgs.git
      pkgs.gnugrep
      pkgs.gnused
      pkgs.gnutar
      pkgs.gzip
      pkgs.kubectl
      pkgs.ripgrep
      pkgs.tini
      pkgs.wget
      pkgs.bubblewrap
    ];
    pathsToLink = [
      "/bin"
      "/share"
    ];
  };
in
pkgs.dockerTools.buildLayeredImage {
  name = "haku-harness-runner";
  tag = null;
  contents = [
    tools
    pkgsUnstable.claude-code
    pkgsMaster.codex
    pkgs.cacert
  ];
  maxLayers = 100;

  enableFakechroot = true;
  fakeRootCommands = ''
    mkdir -p home/runner workspace tmp etc/ssl/certs opt usr/bin usr/local/bin
    chmod 1777 tmp
    ln -s ${pkgs.coreutils}/bin/env usr/bin/env

    # Keep the paths consumed by the runner contract stable while the underlying
    # binaries come from the flake's pinned package sets.
    ln -s ${pkgsUnstable.claude-code}/bin/claude usr/local/bin/claude
    ln -s ${pkgsMaster.codex} opt/codex
    ln -s ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt

    chown -R 1000:1000 home/runner workspace

    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/bash
    runner:x:1000:1000:Runner:/home/runner:/bin/bash
    nobody:x:65534:65534:Nobody:/:/noshell
    PASSWD
    cat > etc/group <<'GROUP'
    root:x:0:
    runner:x:1000:
    nogroup:x:65534:
    GROUP
    cat > etc/nsswitch.conf <<'NSS'
    passwd: files
    group: files
    hosts: files dns
    NSS
  '';

  config = {
    Entrypoint = [ "${runner}/bin/haku-runner" ];
    Env = [
      "PATH=/bin:${tools}/bin"
      "HAKU_CLAUDE_PATH=/usr/local/bin/claude"
      "HAKU_CODEX_PATH=/opt/codex/bin/codex"
      "HOME=/home/runner"
      "CLAUDE_CONFIG_DIR=/claude-config"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
    ];
    User = "1000:1000";
    WorkingDir = "/workspace";
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
  };
}
