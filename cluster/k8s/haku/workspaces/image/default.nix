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
# STATUS (2026-07-26): Bazel now uses nixpkgs' build rather than a bazelisk download,
# which is what makes it work here. Runtime re-probe pending. Still not what the
# SandboxTemplate pulls — see <README.md>.
#
# THE PROBLEM, AND THE THREE THINGS THAT DID NOT SOLVE IT.
# NixOS glibc compiles nix-store paths into its library search path, so binaries downloaded
# at runtime cannot find `libstdc++.so.6`. Bazel's own extracted helpers (process-wrapper,
# linux-sandbox) are exactly that. Measured, in order:
#   1. LD_LIBRARY_PATH in the image — Bazel does not pass it to process-wrapper.
#   2. nix-ld + NIX_LD re-injected via --host_action_env/--repo_env, i.e. what
#      <../../../../../nix/nixos/modules/bazel/default.nix> does on real NixOS hosts. Still
#      no good in general: --verbose_failures shows rules_python's PyWriteBuildData running
#      `exec env - …`, which clears the environment outright.
#   3. patchelf-ing the extracted helpers — impossible. Bazel checksums its install base:
#      "FATAL: corrupt installation: file '.../process-wrapper' is missing or modified."
#   (An /etc/ld.so.cache is also out: nixpkgs glibc reads its cache from inside its own
#   read-only store path, so ldconfig cannot write one.)
#
# The fix is to never have an unpatched Bazel in the first place: nixpkgs' bazel_8 patches
# those helpers at build time. See `bazelPkg` below for the .bazelversion caveat.
#
# nix-ld and /etc/bazel.bazelrc are KEPT even so — rules_python still fetches a hermetic
# CPython at runtime, which is the same class of binary, and those two are what our NixOS
# hosts use to cope with it.
#
# A full-NixOS container (like nixosConfigurations.nix-rbe-worker) would get envfs/nix-ld
# for free, but cannot boot here per
# <../../../../../haku/runtime/managed_agent/self_hosted/README.md>: systemd PID 1 in an
# unprivileged container can't mount the API filesystems, which is why the Haku
# managed-agent image runs its closure directly rather than booting. Hence static
# `/usr/bin/env` and `/bin/bash` symlinks instead of envfs.
#
# Build:  nix build .#haku-sandbox-image
# Load:   docker load < result
{ pkgs }:
let
  # nixpkgs' Bazel, NOT bazelisk. This is the crux of making Bazel work here at all.
  #
  # bazelisk downloads an upstream Bazel release, whose embedded helpers (process-wrapper,
  # linux-sandbox) are ordinary FHS binaries that cannot find libstdc++ under NixOS glibc.
  # There is no way to fix them after the fact: Bazel CHECKSUMS its extracted install base
  # and refuses to start if anything is modified —
  #   FATAL: corrupt installation: file '.../process-wrapper' is missing or modified.
  # (measured 2026-07-26, which is what killed the patchelf-the-helpers approach).
  # nixpkgs' bazel patches those helpers at build time, so the install base it extracts is
  # both correct for Nix and self-consistent.
  #
  # DEVIATION worth knowing: this pins the sandbox to nixpkgs' Bazel and IGNORES
  # haku-state's `.bazelversion` (only bazelisk reads that file). nixpkgs bazel_8 is 8.7.0
  # against haku-state's pinned 8.6.0 — a patch-level skew from CI, which still uses
  # bazelisk. Keep them aligned deliberately: either bump `.bazelversion` to match nixpkgs
  # or accept the skew knowingly.
  bazelPkg = pkgs.bazel_8;

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
      bazelPkg
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
    # linked ELF binaries that hard-code this interpreter path.
    # nix-ld, not glibc's own loader. Same role the `programs.nix-ld.enable` half of
    # <../../../../../nix/nixos/modules/bazel/default.nix> plays on our NixOS hosts: a stub
    # at the FHS loader path that resolves libraries from NIX_LD_LIBRARY_PATH, so
    # dynamically-linked binaries Bazel downloads can start. Plain glibc's loader gets them
    # as far as exec but not as far as finding libstdc++.
    ln -sf ${pkgs.nix-ld}/libexec/nix-ld lib64/ld-linux-x86-64.so.2

    # envfs is the module's third mechanism and is the one that CANNOT be ported: it is a
    # FUSE mount needing systemd activation, and this repo already established that an
    # unprivileged pod cannot boot systemd (haku/runtime/managed_agent/self_hosted/README.md
    # — "booting systemd PID 1 in an unprivileged container can't mount the API
    # filesystems"). Static symlinks cover what actually gets used: `/usr/bin/env` for
    # shebangs and `/bin/bash` for Bazel's shell.
    mkdir -p usr/bin
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
    ln -sf ${pkgs.bashInteractive}/bin/bash bin/bash

    # The module's /etc/bazel.bazelrc, adapted: same NIX_LD passthrough, but pointed at this
    # image's paths instead of /run/current-system/sw/bin. The passthrough lines are the
    # whole point — Bazel scrubs its environment before spawning helpers and actions, so
    # NIX_LD/NIX_LD_LIBRARY_PATH have to be re-injected explicitly or nix-ld's stub receives
    # nothing and dies exactly like the bare loader did.
    cat > etc/bazel.bazelrc <<'BAZELRC'
    build --shell_executable=/bin/bash
    build --host_action_env=PATH=/bin:/usr/bin:/usr/local/bin:/sbin
    test --test_env=PATH=/bin:/usr/bin:/usr/local/bin
    build --host_action_env=NIX_LD
    build --host_action_env=NIX_LD_LIBRARY_PATH
    common --repo_env=NIX_LD
    common --repo_env=NIX_LD_LIBRARY_PATH
    BAZELRC


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
      # What nix-ld's stub loader reads. LD_LIBRARY_PATH alone is not enough because Bazel
      # strips it; these survive via the --host_action_env/--repo_env lines in
      # /etc/bazel.bazelrc above.
      "NIX_LD=${pkgs.glibc}/lib/ld-linux-x86-64.so.2"
      "NIX_LD_LIBRARY_PATH=${runtimeLibs}"
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
