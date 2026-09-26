# What a `dockerTools` image needs for an FHS binary to run in it: a dynamically linked ELF
# that hard-codes `/lib64/ld-linux-x86-64.so.2` and looks for libstdc++ and libz where an FHS
# distro keeps them. rules_python's hermetic CPython, the agent harness CLIs and every toolchain
# Bazel downloads are such binaries. It also gives a shell started without an environment the
# PATH an FHS distro's would have. The rule behind this file (port FILESYSTEM defaults, not
# environment variables) and every measured dead end are recorded once, in
# <../../devinfra/debug/nixos_bazel_bash/README.md> "Two substrates" and "Issue 4".
#
# An image puts `nixLdLibraries` in its `buildEnv` (with `/share` among `pathsToLink`), runs
# `fakeRootCommands` in its own once `/bin` exists, and appends `env` to `config.Env`.
{ pkgs }:
let
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
{
  # nix-ld's ENV-INDEPENDENT FALLBACK. This is the load-bearing piece, and the thing an
  # earlier revision of the Haku image missed while copying the NixOS module's env vars.
  #
  # nix-ld has two compiled-in defaults (read out of the 2.0.6 binary's strings on wyrm2):
  #     /run/current-system/sw/share/nix-ld/lib/ld.so   — the real loader
  #     /run/current-system/sw/share/nix-ld/lib         — its library search path
  # It consults those when NIX_LD / NIX_LD_LIBRARY_PATH are absent. That is why a NixOS host
  # runs FHS binaries fine with NIX_LD unset AND under `env -`, while an image with the same
  # nix-ld store path, byte for byte, aborted the moment anything scrubbed the environment.
  # `programs.nix-ld.enable` sets the env vars only in `environment.sessionVariables`, which
  # reach login shells and not systemd services; the filesystem is the real mechanism.
  #
  # Reproduces nixpkgs' `nix-ld-libraries` buildEnv (nixos/modules/programs/nix-ld.nix)
  # verbatim in shape; `fakeRootCommands` then puts it where nix-ld already looks. With this,
  # NO environment passthrough is needed for the loader to work at all.
  nixLdLibraries = pkgs.buildEnv {
    name = "nix-ld-libraries";
    paths = map pkgs.lib.getLib runtimeLibPkgs;
    pathsToLink = [ "/lib" ];
    extraPrefix = "/share/nix-ld";
    ignoreCollisions = true;
    postBuild = ''
      ln -s ${pkgs.stdenv.cc.bintools.dynamicLinker} $out/share/nix-ld/lib/ld.so
    '';
  };

  fakeRootCommands = ''
    mkdir -p tmp lib64 usr/bin
    chmod 1777 tmp

    # Put nix-ld's fallback exactly where its compiled-in default expects it. An
    # unprivileged pod cannot create /run at runtime (measured: "mkdir: cannot create
    # directory '/run': Permission denied"), so it has to exist in the image.
    mkdir -p run/current-system/sw/share
    ln -s /share/nix-ld run/current-system/sw/share/nix-ld

    # FHS dynamic loader: nix-ld, not glibc's own loader. Same role the `programs.nix-ld.enable`
    # half of <../nixos/modules/bazel/default.nix> plays on our NixOS hosts: a stub at the FHS
    # loader path that resolves libraries from NIX_LD_LIBRARY_PATH (or the fallback above).
    # Plain glibc's loader gets such binaries as far as exec but not as far as finding
    # libstdc++.
    ln -sf ${pkgs.nix-ld}/libexec/nix-ld lib64/ld-linux-x86-64.so.2

    # envfs is the NixOS module's third mechanism and is the one that CANNOT be ported: it is a
    # FUSE mount needing systemd activation, and an unprivileged pod cannot boot systemd
    # (<../../haku/runtime/managed_agent/self_hosted/README.md> — "booting systemd PID 1 in an
    # unprivileged container can't mount the API filesystems"). Static symlinks cover what
    # actually gets used: `/usr/bin/env` for shebangs, and `/bin/bash` and `/bin/sh` for Bazel's
    # shell and every `#!/bin/sh` or `#!/bin/bash` tool. Both are nixpkgs' FHS build of bash:
    # started without PATH, as Bazel starts every action whose rule declares no environment, it
    # searches `/usr/bin` and `/bin`, where the default build searches `/no-such-path`.
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
    ln -sf ${pkgs.bashInteractiveFHS}/bin/bash bin/bash
    ln -sf ${pkgs.bashInteractiveFHS}/bin/sh bin/sh
  '';

  env = [
    "LD_LIBRARY_PATH=${runtimeLibs}"
    # What nix-ld's stub loader reads while the environment survives; Bazel strips
    # LD_LIBRARY_PATH from actions, and `nixLdLibraries` covers a scrubbed environment.
    "NIX_LD=${pkgs.glibc}/lib/ld-linux-x86-64.so.2"
    "NIX_LD_LIBRARY_PATH=${runtimeLibs}"
  ];
}
