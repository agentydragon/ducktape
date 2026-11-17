# Experimental: Nix-based BuildBuddy RBE container image for actions.
#
# The production RBE container image extends BuildBuddy's Ubuntu executor image
# (devinfra/buildbuddy/images/rbe/Dockerfile). This experiment builds an alternative
# action container image with Nix dockerTools. It is NOT production-ready.
#
# BuildBuddy Firecracker replaces /run, so nix-ld's usual filesystem defaults
# must point at the image's permanent /share/nix-ld directory. See README.md.
#
# Build:  nix build .#nix-rbe-container-image
# Load:   docker load < result
# Test:   docker run --rm nix-rbe-container-image bash -c 'gcc --version'
#
# BuildBuddy requirements:
# - No ENTRYPOINT (BB execs commands directly via CMD)
# - Docker CE available (BB starts dockerd via init-dockerd on Firecracker)
# - Non-root "buildbuddy" user with passwordless sudo + docker group
# - /bin/bash must exist (Bazel hardcodes it)
# - Standard build tools (gcc, binutils, make, git, python3, java)
{ pkgs, runnerTools }:
let
  nixLdImage = import ../../../../../../nix/lib/nix-ld-image.nix { inherit pkgs; };
  # nix-ld 2.0.6 hardcodes /run/current-system/sw/share/nix-ld as the fallback
  # when an action clears its environment. Firecracker mounts a fresh /run.
  nixLd = pkgs.nix-ld.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      substituteInPlace src/main.rs \
        --replace-fail /run/current-system/sw/share/nix-ld/lib /share/nix-ld/lib
    '';
  });
  packages = (import ../../../nix_packages.nix { inherit pkgs; }) ++ [
    runnerTools
    pkgs.util-linux
    pkgs.hostname
    pkgs.pam
  ];

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

  # Use nix-ld as the FHS interpreter for downloaded binaries.
  fhsLibs = pkgs.runCommand "fhs-libs" { } ''
    mkdir -p $out/lib64 $out/lib/x86_64-linux-gnu $out/usr/lib/x86_64-linux-gnu

    # FHS interpreter with Nix library defaults
    ln -s ${nixLd}/libexec/nix-ld $out/lib64/ld-linux-x86-64.so.2

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
    ln -s ${pkgs.bashInteractiveFHS}/bin/bash $out/bin/bash
    ln -s ${pkgs.bashInteractiveFHS}/bin/sh $out/bin/sh
    ln -s ${pkgs.bashInteractiveFHS}/bin/bash $out/usr/bin/bash
    ln -s ${pkgs.bashInteractiveFHS}/bin/sh $out/usr/bin/sh
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
  name = "nix-rbe-container-image";
  tag = "latest";
  maxLayers = 10;

  contents = pkgs.buildEnv {
    name = "rbe-root";
    paths = [
      (pkgs.lib.setPrio 10 env)
      fhsLibs
      fhsLinks
      dockerdWrapper
      nixLdImage.nixLdLibraries
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

    # Match the standard FHS layout. Firecracker supplies a fresh /run, and
    # init-dockerd creates its root:docker mode-0660 socket through /var/run.
    rm -rf var/run
    ln -s /run var/run

    # BuildBuddy's run-under wrapper calls /bin/sudo as buildbuddy. The
    # buildEnv entry is a symlink into the immutable Nix store, where it is
    # neither root-owned nor setuid; replace it with a real rootfs copy.
    rm -f bin/sudo
    cp ${pkgs.sudo}/bin/sudo bin/sudo
    chown 0:0 bin/sudo
    chmod 4755 bin/sudo

    mkdir -p etc/sudoers.d etc/pam.d etc/ssl/certs etc/pki/tls/certs

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
    Defaults secure_path="/bin:/usr/bin"
    %wheel ALL=(ALL:ALL) NOPASSWD: ALL
    SUDOERS
    chmod 440 etc/sudoers

    # Nixpkgs' sudo uses PAM, but this plain dockerTools image has no
    # NixOS-generated /etc/pam.d/sudo. The sudoers policy grants passwordless
    # access to wheel members, and this image's wheel group contains only
    # buildbuddy. PAM handles root and non-root invocations without relying on
    # an absent OS account stack.
    cat > etc/pam.d/sudo <<'PAM'
    #%PAM-1.0
    auth sufficient ${pkgs.pam}/lib/security/pam_rootok.so
    auth required ${pkgs.pam}/lib/security/pam_deny.so
    account required ${pkgs.pam}/lib/security/pam_permit.so
    session required ${pkgs.pam}/lib/security/pam_permit.so
    PAM

    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-bundle.crt
    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/pki/tls/certs/ca-bundle.crt

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
    ]
    ++ nixLdImage.env;
  };
}
