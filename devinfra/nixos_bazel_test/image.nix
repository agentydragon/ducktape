# Docker image from the flake's bazel-test NixOS configuration.
#
# Uses the NixOS config defined in nix/flake.nix (nixosConfigurations.bazel-test),
# which includes bazel-dev.nix (system packages, nix-ld) and home-manager with
# nixos-bazel.nix (~/.bazelrc) and direnv. Config content (bazelrc, direnvrc) is
# read from the evaluated NixOS/home-manager config — not hardcoded here.
#
# NixOS activation scripts can't run inside a nix build sandbox, so Docker image
# plumbing (/run/current-system/sw, /lib64, /etc) is set up manually.
#
# Build (from repo root):
#   nix build path:./nix#bazel-test-docker -o devinfra/nixos_bazel_test/result
# Load:
#   docker load < devinfra/nixos_bazel_test/result
# Run:
#   docker run --rm -it --network=host -v $PWD:/repo -w /repo ducktape-nixos-bazel bash
{ nixos, pkgs }:
let
  # NixOS system profile — all packages merged into /bin, /lib, etc.
  nixosPath = nixos.config.system.path;

  # Home-manager evaluated config for root user
  hmRoot = nixos.config.home-manager.users.root;
  userBazelrc = pkgs.writeText "bazelrc" hmRoot.home.file.".bazelrc".text;
  userDirenvrc = pkgs.writeText "direnvrc" hmRoot.programs.direnv.stdlib;

  # nix-ld paths
  glibcLib = "${pkgs.glibc}/lib";
  gccLib = "${pkgs.stdenv.cc.cc.lib}/lib";
  nixLdPath = "${pkgs.glibc}/lib/ld-linux-x86-64.so.2";

in
pkgs.dockerTools.buildLayeredImage {
  name = "ducktape-nixos-bazel";
  tag = "latest";

  contents = [ nixosPath ];

  fakeRootCommands = ''
        # /run/current-system/sw → NixOS system profile.
        # Normally created by switch-to-configuration; manual in Docker images.
        mkdir -p ./run/current-system
        ln -s ${nixosPath} ./run/current-system/sw

        # /usr/bin/env for #!/usr/bin/env shebangs
        mkdir -p ./usr/bin
        ln -s ${pkgs.coreutils}/bin/env ./usr/bin/env

        # nix-ld: dynamic linker stub at the standard path
        mkdir -p ./lib64
        ln -sf ${pkgs.nix-ld}/bin/nix-ld ./lib64/ld-linux-x86-64.so.2

        # /etc basics
        mkdir -p ./etc/ssl/certs ./etc/profile.d
        ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt ./etc/ssl/certs/ca-certificates.crt
        ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt ./etc/ssl/certs/ca-bundle.crt
        echo "root:x:0:0:root:/root:/bin/bash" > ./etc/passwd
        echo "root:x:0:" > ./etc/group

        # nix-ld env setup (sourced by bashrc)
        cat > ./etc/profile.d/nix-ld.sh << 'NIXLD'
    export NIX_LD=${nixLdPath}
    export NIX_LD_LIBRARY_PATH=${glibcLib}:${gccLib}:${nixosPath}/lib
    NIXLD

        # Home directory with configs from home-manager evaluated config
        mkdir -p ./root/.config/bazel ./root/.config/direnv
        cp ${userBazelrc} ./root/.bazelrc
        echo 'try-import /root/.config/bazel/buildbuddy.bazelrc' >> ./root/.bazelrc
        cp ${userDirenvrc} ./root/.config/direnv/direnvrc
        cat > ./root/.bashrc << 'BASHRC'
    . /etc/profile.d/nix-ld.sh
    BASHRC

        mkdir -p ./tmp
        chmod 1777 ./tmp
  '';

  enableFakechroot = true;

  config = {
    Cmd = [ "${pkgs.bash}/bin/bash" ];
    WorkingDir = "/repo";
    Env = [
      "PATH=${nixosPath}/bin:${nixosPath}/sbin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "NIX_LD=${nixLdPath}"
      "NIX_LD_LIBRARY_PATH=${glibcLib}:${gccLib}:${nixosPath}/lib"
      "HOME=/root"
      "USER=root"
      "TERM=xterm-256color"
    ];
  };
}
