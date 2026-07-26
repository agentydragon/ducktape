# Nix-built OCI image for the Haku sandbox — the Nix replacement for the Dockerfile
# beside it. Same job (see that file's header): an in-cluster EXEC TARGET where an agent
# runs `bazel run //cli:...` / `bazel test //...` against a git-synced haku-state checkout,
# driven through haku/sandbox_mcp.
#
# Why Nix: the Dockerfile's tool set is an `apt-get install` line nobody revisits, and it
# has now produced the same class of bug three times — a missing `kubectl`, then a
# `python3-minimal` whose absent `json`/`shutil` broke both `tools/ci_wait.sh` and the
# agent's own heredoc editing motion, then a missing `jq`/`tea`. As a Nix attribute set the
# tool list is one reviewable list, and it shares a substrate with the other agent pod image
# (<../../../../../x/codex_pod_image/default.nix>) instead of re-deriving it in apt.
# `tini` as PID 1 also reaps the exec zombies a long-lived claim accumulates (a run driven
# through many backgrounded `exec_sandbox` calls left 5 in one session).
#
# STATUS (2026-07-25): NOT yet the image the SandboxTemplate pulls. It builds in CI, but the
# risk below is a RUNTIME one that only a real pod can settle, so cutting over is gated on
# the checklist in <README.md> § Cutting over to the Nix image.
#
# THE RISK, and why it is not the same one that killed the RBE image.
# <../../../../../x/nix_rbe_image/README.md> records that NixOS glibc compiles nix-store
# paths into its library search path, so dynamically-linked binaries *downloaded at runtime*
# cannot find `libstdc++.so.6`. This image is the worst case for that: its whole job is
# bazelisk fetching Bazel (which runs a bundled JDK) and rules_python fetching a hermetic
# CPython — both downloaded, both dynamically linked. But the RBE blocker was specifically
# that BuildBuddy's Firecracker goinit never runs the container's `/init`, so nothing could
# ever set the compat env. A SandboxTemplate pod has no such constraint: we own the image
# config and the pod spec, so the loader symlinks and LD_LIBRARY_PATH below are simply
# present. That is the bet this file makes, and the cutover checklist is how it gets tested.
#
# Build:  nix build .#haku-sandbox-image
# Load:   docker load < result
{ pkgs }:
let
  # Everything the run needs on PATH. Mirrors the Dockerfile's apt list plus the two tools
  # its absence kept costing (jq, tea); each line notes what breaks without it.
  hakuSandboxEnv = pkgs.buildEnv {
    name = "haku-sandbox-env";
    paths = [
      pkgs.bashInteractive
      pkgs.coreutils
      pkgs.findutils
      pkgs.gnugrep
      pkgs.gnused
      pkgs.gawk
      pkgs.gnutar
      pkgs.gzip
      pkgs.diffutils
      pkgs.procps # haku-sandbox-setup.sh + agent diagnostics (ps/pgrep)
      pkgs.less
      pkgs.which

      pkgs.git # haku-state + ducktape checkouts, the run's commits
      pkgs.curl
      pkgs.openssl # haku-sandbox-setup.sh §1 splits the egress CA bundle with it
      pkgs.cacert
      pkgs.jq
      pkgs.tea # procedures/code_changes.md routes code changes through Forgejo PRs
      pkgs.kubectl # tools/plaid_q.sh, cli/k8s_secrets.py, pod-driven source reads

      # FULL python3 — never `python3Minimal`. The run's own deps come from Bazel, but the
      # stdlib here is load-bearing twice over: tools/ci_wait.sh parses the Forgejo runs API
      # with it, and an agent driving this box edits files with heredoc'd python.
      pkgs.python3

      pkgs.bazelisk # reads haku-state's .bazelversion (-> Bazel 8.6.0) and fetches it
      pkgs.jdk_headless # keytool for the egress truststore; also a JVM for the Bazel server

      # haku-state's cc rules resolve through Bazel's local_config_cc autoconf, which probes
      # for a system gcc/g++. Without a C toolchain any target pulling it (e.g. //cli:bookmark)
      # fails analysis with "Cannot find gcc or CC".
      pkgs.gcc
      pkgs.gnumake
      pkgs.binutils

      pkgs.tini # PID 1: reaps the zombies abandoned `exec_sandbox` calls leave behind
    ];
    pathsToLink = [
      "/bin"
      "/share"
      "/lib"
      "/include"
    ];
  };

  # Shared objects the runtime-downloaded binaries look for. Bazel's bundled JDK wants
  # libstdc++/libz/libgcc; python-build-standalone wants libz and friends.
  runtimeLibs = pkgs.lib.makeLibraryPath [
    pkgs.stdenv.cc.cc.lib # libstdc++.so.6, libgcc_s.so.1
    pkgs.zlib
    pkgs.glibc
    pkgs.openssl
  ];
in
pkgs.dockerTools.buildLayeredImage {
  name = "haku-sandbox";
  # Content-addressed; CI adds the sortable tag the SandboxTemplate/ImagePolicy selects on.
  tag = null;

  contents = hakuSandboxEnv;
  maxLayers = 100;

  enableFakechroot = true;
  fakeRootCommands = ''
    mkdir -p tmp workspace etc/ssl/certs lib64 usr/local/bin
    chmod 1777 tmp

    # FHS dynamic loader. bazelisk itself is a static Go binary and runs anywhere, but the
    # Bazel it downloads (and rules_python's hermetic CPython) are ordinary dynamically
    # linked ELF binaries that hard-code this interpreter path. Without the symlink they
    # die "no such file or directory" — which reads as a missing binary, not a missing loader.
    ln -sf ${pkgs.glibc}/lib/ld-linux-x86-64.so.2 lib64/ld-linux-x86-64.so.2

    # The reviewed per-claim bootstrap, same file the Dockerfile bakes — one copy, so the
    # Nix and apt images cannot drift in what they do to a fresh claim.
    cp ${./haku-sandbox-setup.sh} usr/local/bin/haku-sandbox-setup.sh
    chmod +x usr/local/bin/haku-sandbox-setup.sh

    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/bash
    workspace:x:1000:1000:Workspace:/home/workspace:/bin/bash
    nobody:x:65534:65534:Nobody:/:/noshell
    PASSWD
    cat > etc/group <<'GROUP'
    root:x:0:
    workspace:x:1000:
    nogroup:x:65534:
    GROUP
    cat > etc/nsswitch.conf <<'NSS'
    passwd: files
    group: files
    hosts: files dns
    NSS

    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt

    mkdir -p home/workspace
    chown -R 1000:1000 home/workspace workspace
  '';

  config = {
    # tini reaps orphaned exec trees; the Dockerfile image has no init and accumulates them.
    Entrypoint = [
      "/bin/tini"
      "--"
    ];
    Cmd = [ "/bin/bash" ];
    User = "1000:1000";
    WorkingDir = "/workspace";
    Env = [
      "PATH=/bin:${hakuSandboxEnv}/bin"
      "HOME=/home/workspace"
      "USER=workspace"
      "LD_LIBRARY_PATH=${runtimeLibs}"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt"
      # The Kyverno egress injection overrides the two CA vars per-pod with the proxy bundle;
      # these are the sane defaults for a bare `docker run` during cutover testing.
    ];
    Labels."org.opencontainers.image.source" = "https://git.allegedly.works/agentydragon/ducktape";
  };
}
