# Nix-built OCI image for the codex pod (see cluster/k8s/agents/codex-pod/README.md).
#
# The tool set is a single `buildEnv` (codexEnv) — adding a tool is a one-line
# edit to `paths`. CI builds this image on `devel` and pushes it; Flux image
# automation rolls the codex Deployment when the digest changes.
#
# Unlike x/nix_rbe_image, this needs no FHS/glibc-search hacks: everything runs
# from the Nix closure (codex, bash, git, …), which already carries correct
# RPATHs. We only plant /bin/bash + /etc/passwd so `kubectl exec` / sshd and the
# non-root UID 1000 work.
#
# Build:  nix build .#codex-pod-image
# Load:   docker load < result
{
  pkgs,
  pkgsUnstable,
}:
let
  # The codex pod's tool set. Mirrors the agent-box codex user
  # (nix/home/hosts/agent-box.nix) plus the shell utilities a dev pod needs.
  codexEnv = pkgs.buildEnv {
    name = "codex-env";
    paths = [
      # Codex CLI (unstable, as home-manager pins it for agent-box).
      pkgsUnstable.codex
      # Shell + core userland.
      pkgs.bashInteractive
      pkgs.coreutils
      pkgs.moreutils
      pkgs.findutils
      pkgs.gnugrep
      pkgs.gnused
      pkgs.gawk
      pkgs.gnutar
      pkgs.gzip
      pkgs.which
      pkgs.psmisc
      # Dev tools.
      pkgs.git
      pkgs.openssh
      pkgs.curl
      pkgs.jq
      pkgs.ripgrep
      pkgs.fd
      pkgs.nodejs_22
      # Nix + direnv so in-pod devshell realization works.
      pkgs.nix
      pkgs.direnv
      pkgs.nix-direnv
      # Cluster access.
      pkgs.kubectl
      # TLS roots.
      pkgs.cacert
    ];
    # Only /bin and /share: /etc is created fresh in fakeRootCommands (linking
    # it here would make it a read-only store symlink we can't write into).
    pathsToLink = [
      "/bin"
      "/share"
    ];
  };
in
pkgs.dockerTools.buildLayeredImage {
  name = "codex-pod";
  # Content-addressed tag: changes iff codexEnv changes (see plan "Roll
  # granularity"). CI adds a sortable tag for Flux ImagePolicy selection.
  tag = null;

  contents = codexEnv;
  maxLayers = 100;

  # /bin/{bash,sh,env} come from the env (contents). Only /etc needs real files
  # here: the OCI runtime resolves /etc/passwd for the UID 1000 user before the
  # /nix overlay is assembled, so it can't be a symlink into the store.
  enableFakechroot = true;
  fakeRootCommands = ''
    mkdir -p tmp home/codex etc/ssl/certs
    chmod 1777 tmp
    chown 1000:1000 home/codex
    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/bash
    codex:x:1000:1000:Codex:/home/codex:/bin/bash
    nobody:x:65534:65534:Nobody:/:/noshell
    PASSWD
    cat > etc/group <<'GROUP'
    root:x:0:
    codex:x:1000:
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
    Env = [
      "PATH=/bin:${codexEnv}/bin"
      "HOME=/home/codex"
      "USER=codex"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt"
      "NIX_CONFIG=experimental-features = nix-command flakes\naccept-flake-config = true"
    ];
    # Source-code provenance (OCI image.source); the image lives in our Forgejo
    # registry, so point at the Forgejo repo.
    Labels."org.opencontainers.image.source" = "https://git.allegedly.works/agentydragon/ducktape";
    # No entrypoint: the Deployment sets the command (start sshd). Default to a
    # shell for `docker run`/ad-hoc use.
    Cmd = [ "/bin/bash" ];
  };
}
