# agentplane's sandbox image: the command-line tools of a box that runs commands, as one list,
# on the nix-ld substrate FHS binaries need (<../../nix/lib/nix-ld-image.nix>). Its user is uid
# 1000 `runner`, home /home/runner. The runner image (<../runner/image.nix>) is this definition
# plus the runner and both harnesses, so a tool added here reaches both.
#
# The egress proxy's CA comes from the pod, not from this image: nothing here sets
# SSL_CERT_FILE, for the reason the Haku image records beside its own `Env`
# (<../../cluster/k8s/haku/workspaces/image/default.nix>).
#
# Build:  nix build .#agentplane-sandbox-image
# Load:   docker load < result
{
  pkgs,
  name ? "agentplane-sandbox",
  # What an image built on this one adds: packages linked into /bin beside the tools, and image
  # config such as an entrypoint.
  extraPaths ? [ ],
  extraConfig ? { },
}:
let
  substrate = import ../../nix/lib/nix-ld-image.nix { inherit pkgs; };

  sandboxEnv = pkgs.buildEnv {
    name = "${name}-env";
    paths = [
      substrate.nixLdLibraries

      pkgs.bashInteractive
      pkgs.coreutils
      pkgs.findutils
      pkgs.gnugrep
      pkgs.gnused
      pkgs.gawk
      pkgs.diffutils
      pkgs.gnupatch
      pkgs.file
      pkgs.less
      pkgs.procps
      pkgs.which
      pkgs.gnutar
      pkgs.gzip
      pkgs.xz
      pkgs.unzip

      pkgs.curl
      pkgs.git
      pkgs.ripgrep
      pkgs.jq
      pkgs.openssl
      pkgs.cacert
      pkgs.kubectl # the sandbox's own Kubernetes identity, through the egress proxy

      # What a local Bazel build compiles and probes with: rules_cc's auto-detected toolchain is
      # this gcc, whose wrapper carries binutils (protoc and protobuf's editions defaults build
      # from source), and aspect_rules_py reads the host libc from `ldd --version`.
      pkgs.gcc
      pkgs.glibc.bin

      # FULL python3, never `python3Minimal`, whose missing `json`/`shutil` broke the Haku image's
      # scripts. nixpkgs marks it EXTERNALLY-MANAGED, so a run installs packages into a
      # `python3 -m venv`, whose ensurepip brings its own pip.
      pkgs.python3
    ]
    ++ extraPaths;
    pathsToLink = [
      "/bin"
      "/share"
      "/lib"
    ];
  };
in
pkgs.dockerTools.buildLayeredImage {
  inherit name;
  # Content-addressed; CI adds a sortable tag, and consumers pin the digest.
  tag = null;

  contents = sandboxEnv;
  maxLayers = 100;

  fakeRootCommands = ''
    ${substrate.fakeRootCommands}
    mkdir -p etc/ssl/certs home/runner

    # uid 1000 `runner`, the user the runner image runs as.
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

    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
    mkdir -p usr/share
    ln -s ${pkgs.tzdata}/share/zoneinfo usr/share/zoneinfo
    chown -R 1000:1000 home/runner
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/home/runner";
    Env = [
      "PATH=/bin:${sandboxEnv}/bin"
      "HOME=/home/runner"
      "USER=runner"
      # nixpkgs' glibc looks for zones under its own store path, which holds none; without this,
      # `TZ=Europe/Prague date` prints UTC. FHS binaries find /usr/share/zoneinfo by default.
      "TZDIR=/usr/share/zoneinfo"
    ]
    ++ substrate.env;
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
  }
  // extraConfig;
}
