# Docker image from the flake's bazel-test NixOS configuration.
#
# Uses the NixOS config defined in nix/flake.nix (nixosConfigurations.bazel-test),
# which includes bazel-dev.nix (system packages, nix-ld) and home-manager with
# nixos-bazel.nix (~/.bazelrc). Packages the system as a Docker image via
# dockerTools.buildLayeredImage.
#
# Build (from repo root):
#   nix build path:./nix#bazel-test-docker -o devinfra/nixos-bazel-test/result
# Load:
#   docker load < devinfra/nixos-bazel-test/result
# Run:
#   docker run --rm -it --network=host -v $PWD:/repo -w /repo ducktape-nixos-bazel bash
{ nixos, pkgs }:
let
  # NixOS system profile — all packages merged into /bin, /lib, etc.
  nixosPath = nixos.config.system.path;

  # Bazelrc content from home-manager config for root user
  hmBazelrc = nixos.config.home-manager.users.root.home.file.".bazelrc";
  userBazelrc = pkgs.writeText "bazelrc" hmBazelrc.text;

  # nix-ld paths
  glibcLib = "${pkgs.glibc}/lib";
  gccLib = "${pkgs.stdenv.cc.cc.lib}/lib";
  nixLdPath = "${pkgs.glibc}/lib/ld-linux-x86-64.so.2";

  direnvrc = pkgs.writeText "direnvrc" ''
    use_nix() {
      : # no-op in container; all packages pre-installed
    }
  '';

in
pkgs.dockerTools.buildLayeredImage {
  name = "ducktape-nixos-bazel";
  tag = "latest";

  # The NixOS system profile (all environment.systemPackages merged)
  contents = [ nixosPath ];

  fakeRootCommands = ''
        # /run/current-system/sw → NixOS system profile (the real thing).
        # On a running NixOS system, switch-to-configuration creates this symlink.
        # In a Docker image we create it directly since activation scripts don't run.
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

        # Home directory with configs
        mkdir -p ./root/.config/bazel ./root/.config/direnv
        cp ${userBazelrc} ./root/.bazelrc
        # Append try-import for BuildBuddy credentials
        echo 'try-import /root/.config/bazel/buildbuddy.bazelrc' >> ./root/.bazelrc
        cp ${direnvrc} ./root/.config/direnv/direnvrc
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
