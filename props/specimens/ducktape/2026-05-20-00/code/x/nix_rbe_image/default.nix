# Experimental: Nix-based BuildBuddy RBE worker image.
#
# The primary RBE image is the Ubuntu Dockerfile with Nix devtools baked in
# (devinfra/rbe_image/Dockerfile). This file is an experiment to build the
# image purely with Nix dockerTools. It is NOT production-ready.
#
# Known limitations of the dockerTools approach:
# - NixOS glibc has nix-store paths compiled into its default library search
#   path and ld.so.cache location. Standard FHS paths (/lib/x86_64-linux-gnu,
#   /usr/lib, /lib64) are not searched by default.
# - nix-ld can bridge this gap, but requires NIX_LD and NIX_LD_LIBRARY_PATH
#   env vars, which BuildBuddy's goinit process does not set.
# - ld.so.cache can partially work (see fakeRootCommands below), but ldconfig
#   at build time runs under fakeroot/fakechroot where path resolution is
#   fragile.
# - The Ubuntu base image avoids all of this because its glibc natively
#   searches FHS paths.
#
# Build:  nix build .#nix-rbe-image
# Load:   docker load < result
# Test:   docker run --rm nix-rbe-worker bash -c 'gcc --version'
#
# BuildBuddy requirements:
# - No ENTRYPOINT (BB execs commands directly via CMD)
# - Docker CE available (BB starts dockerd via init-dockerd on Firecracker)
# - Non-root "buildbuddy" user with passwordless sudo + docker group
# - /bin/bash must exist (Bazel hardcodes it)
# - Standard build tools (gcc, binutils, make, git, python3, java)
{ pkgs }:
let
  packages = import ./packages.nix { inherit pkgs; };

  # Merged environment with all packages on PATH.
  env = pkgs.buildEnv {
    name = "rbe-env";
    paths = packages;
    pathsToLink = [
      "/bin"
      "/lib"
      "/lib64"
      "/include"
      "/share"
      "/etc"
    ];
  };

  # FHS library layout: place the dynamic linker at /lib64/ and shared libs
  # at Debian multiarch paths. NixOS glibc does NOT search these paths by
  # default (see file header), so we also generate ld.so.cache in
  # fakeRootCommands to tell the linker about them.
  fhsLibs = pkgs.runCommand "fhs-libs" { } ''
    mkdir -p $out/lib64 $out/lib/x86_64-linux-gnu $out/usr/lib/x86_64-linux-gnu

    # Real glibc dynamic linker
    ln -s ${pkgs.glibc}/lib/ld-linux-x86-64.so.2 $out/lib64/ld-linux-x86-64.so.2

    # Shared libraries in Debian multiarch paths
    for dir in \
      ${pkgs.stdenv.cc.cc.lib}/lib \
      ${pkgs.glibc}/lib \
      ${pkgs.zlib}/lib \
      ${pkgs.openssl.out}/lib; do
      for lib in "$dir"/lib*.so*; do
        if [ -e "$lib" ]; then
          ln -sf "$lib" $out/lib/x86_64-linux-gnu/
          ln -sf "$lib" $out/usr/lib/x86_64-linux-gnu/
        fi
      done
    done
  '';

  # FHS symlinks: /bin/bash, /bin/sh, /usr/bin/env — Bazel hardcodes these.
  # Also bazel -> bazelisk (matching Ubuntu base image convention).
  fhsLinks = pkgs.runCommand "fhs-links" { } ''
    mkdir -p $out/bin $out/usr/bin
    ln -s ${pkgs.bash}/bin/bash $out/bin/bash
    ln -s ${pkgs.bash}/bin/sh $out/bin/sh
    ln -s ${pkgs.coreutils}/bin/env $out/usr/bin/env
    ln -s ${pkgs.bazelisk}/bin/bazelisk $out/bin/bazel
  '';

  # Dockerd wrapper for Firecracker VMs lacking CONFIG_IP_NF_RAW.
  dockerdWrapper = pkgs.writeShellScriptBin "dockerd-wrapper" ''
    export DOCKER_INSECURE_NO_IPTABLES_RAW=1
    exec ${pkgs.docker}/bin/dockerd "$@"
  '';

in
pkgs.dockerTools.buildLayeredImage {
  name = "nix-rbe-worker";
  tag = "latest";

  contents = pkgs.buildEnv {
    name = "rbe-root";
    paths = [
      (pkgs.lib.setPrio 10 env)
      fhsLibs
      fhsLinks
      dockerdWrapper
    ];
    pathsToLink = [
      "/bin"
      "/lib"
      "/lib64"
      "/include"
      "/share"
      "/usr"
    ];
  };

  # Extra commands run as root during image build (before layers are sealed).
  # fakeRootCommands runs under fakeroot, allowing chown without real root.
  # /etc files are created here as real files (not symlinks into /nix/store)
  # because BuildBuddy's OCI runtime resolves /etc/passwd before the full
  # rootfs overlay is assembled, so symlinks into /nix/store don't work.
  fakeRootCommands = ''
    mkdir -p home/buildbuddy tmp var/tmp run
    chmod 1777 tmp var/tmp
    chown 1000:1000 home/buildbuddy

    mkdir -p etc/sudoers.d etc/ssl/certs etc/pki/tls/certs

    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/bash
    buildbuddy:x:1000:1000:BuildBuddy:/home/buildbuddy:/bin/bash
    nobody:x:65534:65534:Nobody:/:/noshell
    PASSWD

    cat > etc/group <<'GROUP'
    root:x:0:
    wheel:x:10:buildbuddy
    docker:x:131:buildbuddy
    users:x:1000:buildbuddy
    nogroup:x:65534:
    GROUP

    cat > etc/shadow <<'SHADOW'
    root:!:1::::::
    buildbuddy:!:1::::::
    nobody:!:1::::::
    SHADOW

    cat > etc/nsswitch.conf <<'NSS'
    passwd: files
    group: files
    shadow: files
    hosts: files dns
    networks: files
    protocols: files
    services: files
    NSS

    cat > etc/sudoers <<'SUDOERS'
    root ALL=(ALL:ALL) ALL
    %wheel ALL=(ALL:ALL) NOPASSWD: ALL
    SUDOERS
    chmod 440 etc/sudoers

    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-bundle.crt
    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/pki/tls/certs/ca-bundle.crt

    # Generate ld.so.cache so NixOS glibc finds libs at Debian multiarch paths.
    cat > etc/ld.so.conf <<'LDCONF'
    /lib/x86_64-linux-gnu
    /usr/lib/x86_64-linux-gnu
    LDCONF
    # fakeRootCommands runs in a fakechroot'd rootfs where /lib/x86_64-linux-gnu
    # exists from the buildEnv contents. ldconfig can scan these real paths.
    ${pkgs.glibc.bin}/bin/ldconfig -f etc/ld.so.conf -C etc/ld.so.cache \
      /lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu 2>/dev/null || true

    cat > etc/bazel.bazelrc <<'BAZELRC'
    build --shell_executable=/bin/bash
    BAZELRC
  '';
  enableFakechroot = true;

  config = {
    User = "buildbuddy";
    WorkingDir = "/home/buildbuddy";
    Env = [
      "PATH=/bin:/usr/bin:${env}/bin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt"
      "JAVA_HOME=${pkgs.jdk11}/lib/openjdk"
    ];
  };
}
