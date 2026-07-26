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
# STATUS (2026-07-26): BLOCKED for Bazel. It builds, pulls, and runs, and everything except
# Bazel works — but `bazel build` cannot execute a single action, and the fix is not in this
# file. Do NOT point the SandboxTemplate at it. Full evidence in <README.md>.
#
# THE BLOCKER, and why the "a pod owns its env" argument was wrong.
# The nix_rbe_image notes record that NixOS glibc compiles nix-store paths into its library
# search path, so dynamically-linked binaries *downloaded at runtime* cannot find
# `libstdc++.so.6`. The bet here was that this only killed the RBE image because
# BuildBuddy's Firecracker goinit never runs the container's `/init`, leaving no way to set
# the compat env — whereas we own this pod spec.
#
# Half of that is right: bazelisk downloads Bazel, extracts it, and its bundled JDK starts
# fine (`Build label: 9.2.0`). But Bazel then SCRUBS THE ENVIRONMENT before spawning its own
# extracted helpers, so `process-wrapper` runs with no LD_LIBRARY_PATH regardless of what
# this image sets, and dies "libstdc++.so.6: cannot open shared object file". Measured: the
# same binary runs WITH the variable and fails WITHOUT it. Owning the pod env buys nothing
# for the one process that needs it.
#
# The usual env-independent escape — an /etc/ld.so.cache built at image-build time — is also
# unavailable: nixpkgs glibc reads its cache from INSIDE its own read-only store path
# (`ldconfig -r .` fails "Can't create temporary cache file
# /nix/store/…-glibc-…/etc/ld.so.cache~: Permission denied"), not from /etc.
#
# So both mechanisms are closed and this needs a different strategy, not another patch here.
# The recommended one is already proven in this repo: <../../../../../devinfra/rbe_image/Dockerfile>
# is a Debian base with a Nix-built tool closure baked in — a normal glibc and a writable
# /etc/ld.so.cache, with the tool list still a single reviewable Nix attribute (which is the
# deduplication the Nix rewrite was for). See README.md § Where this goes next.
#
# Kept in-tree, building in CI, and publishing to an unconsumed image name because the
# diagnosis above is worth preserving and the remaining fixes here are all correct.
#
# Build:  nix build .#haku-sandbox-image
# Load:   docker load < result
{ pkgs }:
let
  # `bazel`, not `bazelisk`. nixpkgs installs the binary under its own name, but every
  # caller in haku-state (tools/*.sh, procedures, CI) invokes `bazel` — the Dockerfile
  # papered over this by curl'ing bazelisk straight to /usr/local/bin/bazel. Measured: with
  # only `bazelisk` on PATH, `bazel version` is command-not-found in the probe pod.
  bazelShim = pkgs.runCommand "bazel-shim" { } ''
    mkdir -p $out/bin
    ln -s ${pkgs.bazelisk}/bin/bazelisk $out/bin/bazel
  '';

  # The per-claim bootstrap, as a Nix package rather than a file copied to /usr/local/bin.
  # Two reasons, both measured in the probe pod: a pure Nix image has no `/usr/bin/env`, so
  # the script's `#!/usr/bin/env bash` shebang dies "bad interpreter" (writeShellScriptBin
  # patches it to an absolute store path); and nothing puts /usr/local/bin on PATH here, so
  # it wasn't runnable by name either. As a package it lands on /bin with everything else.
  # The .sh file stays the single source — the Dockerfile still COPYs it verbatim.
  hakuSandboxSetup = pkgs.writeShellScriptBin "haku-sandbox-setup" (
    builtins.readFile ./haku-sandbox-setup.sh
  );

  # Everything the run needs on PATH. Mirrors the Dockerfile's apt list plus the two tools
  # its absence kept costing (jq, tea); each line notes what breaks without it.
  hakuSandboxEnv = pkgs.buildEnv {
    name = "haku-sandbox-env";
    paths = [
      bazelShim
      hakuSandboxSetup

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

  # Shared objects the runtime-downloaded binaries look for. Bazel's own helpers
  # (process-wrapper, linux-sandbox) want libstdc++/libgcc; python-build-standalone wants
  # libz and friends.
  runtimeLibPkgs = [
    pkgs.stdenv.cc.cc.lib # libstdc++.so.6, libgcc_s.so.1
    pkgs.zlib
    pkgs.glibc
    pkgs.openssl.out
  ];
  runtimeLibs = pkgs.lib.makeLibraryPath runtimeLibPkgs;
in
pkgs.dockerTools.buildLayeredImage {
  name = "haku-sandbox";
  # Content-addressed; CI adds the sortable tag the SandboxTemplate/ImagePolicy selects on.
  tag = null;

  contents = hakuSandboxEnv;
  maxLayers = 100;

  enableFakechroot = true;
  fakeRootCommands = ''
    mkdir -p tmp workspace etc/ssl/certs lib64
    chmod 1777 tmp

    # FHS dynamic loader. bazelisk itself is a static Go binary and runs anywhere, but the
    # Bazel it downloads (and rules_python's hermetic CPython) are ordinary dynamically
    # linked ELF binaries that hard-code this interpreter path. Without the symlink they
    # die "no such file or directory" — which reads as a missing binary, not a missing loader.
    ln -sf ${pkgs.glibc}/lib/ld-linux-x86-64.so.2 lib64/ld-linux-x86-64.so.2


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
    # GOTCHA: a Kubernetes `command:` overrides ENTRYPOINT outright, so this is inert unless
    # the pod cooperates — and `sandboxtemplate-haku.yaml` currently sets
    # `command: ["sleep", "infinity"]`, which would bypass tini entirely (verified in the
    # probe pod: PID 1 was coreutils, not tini). At cutover, change the template to
    # `["/bin/tini", "--", "sleep", "infinity"]` or move it to `args:`, or the zombie
    # reaping this package is here for does not happen.
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
      # Deliberately NO GIT_SSL_CAINFO and NO SSL_CERT_FILE baked in. Setting them to the
      # public cacert bundle looks like a harmless default but is an active bug in-cluster:
      # the egress proxy bumps every external host, its CA is NOT in that bundle, and
      # GIT_SSL_CAINFO takes precedence over the `http.sslCAInfo` git config that
      # haku-sandbox-setup.sh sets. Measured 2026-07-26 — with it baked in, cloning ducktape
      # failed `unable to get local issuer certificate (20)` while `git ls-remote` with the
      # var unset succeeded against the same host. The Kyverno egress injection supplies
      # SSL_CERT_FILE/CURL_CA_BUNDLE/REQUESTS_CA_BUNDLE pointing at the proxy bundle, and
      # the bootstrap covers git; leave both to the pod.
    ];
    Labels."org.opencontainers.image.source" = "https://git.allegedly.works/agentydragon/ducktape";
  };
}
