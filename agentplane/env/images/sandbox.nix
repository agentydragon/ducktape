# agentplane's sandbox image: the command-line tools of a box that runs commands (<tools.nix>, which
# the VM guest shares) on the nix-ld substrate FHS binaries need (<../../../nix/lib/nix-ld-image.nix>).
# Its user is uid 1000 `runner`, home /home/runner. The runner image (<./image.nix>) is this
# definition plus both harnesses, and the build image (<build.nix>) this definition plus Bazel and
# what a build compiles with, so a tool added to <tools.nix> reaches all three and the VM guest.
#
# The egress proxy's CA comes from the pod, not from this image: nothing here sets SSL_CERT_FILE
# or GIT_SSL_CAINFO. Pointing them at the public cacert bundle breaks TLS in-cluster, since the
# proxy re-signs every external host with a CA that bundle lacks, and GIT_SSL_CAINFO overrides
# git's `http.sslCAInfo`, so the pod's git configuration cannot repair it.
#
# Build:  nix build .#agentplane-sandbox-image
# Load:   docker load < result
{
  pkgs,
  # The released `agentplane` command (<../../cli/package.nix>), for an image that carries its own.
  # The runner image takes it from the runner disk instead (//agentplane/env/images:disk_image).
  agentplaneCli ? null,
  name ? "agentplane-sandbox",
  # What an image built on this one adds: packages linked into /bin beside the tools, directories
  # appended to PATH, and image config such as an entrypoint.
  extraPaths ? [ ],
  extraPathEntries ? [ ],
  extraConfig ? { },
}:
let
  substrate = import ../../../nix/lib/nix-ld-image.nix { inherit pkgs; };

  sandboxEnv = pkgs.buildEnv {
    name = "${name}-env";
    paths = [
      substrate.nixLdLibraries

      pkgs.bashInteractiveFHS # the substrate's /bin/bash; the default build has no PATH fallback
      pkgs.cacert
    ]
    ++ import ./tools.nix pkgs
    # The Action, Notification and egress-rules APIs (<../../docs/agent_cli.md>).
    ++ pkgs.lib.optional (agentplaneCli != null) agentplaneCli
    ++ extraPaths;
    pathsToLink = [
      "/bin"
      "/share"
      "/lib"
    ];
  };
  pathEntries = [
    "/bin"
    "${sandboxEnv}/bin"
  ]
  ++ extraPathEntries;
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
      "PATH=${pkgs.lib.concatStringsSep ":" pathEntries}"
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
