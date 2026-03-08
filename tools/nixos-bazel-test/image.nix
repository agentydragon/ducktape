# Docker image from a real NixOS system configuration.
#
# Uses NixOS eval-config.nix with boot.isContainer to build a genuine NixOS
# system, then packages it as a Docker image via dockerTools.buildLayeredImage.
# The resulting container has real NixOS filesystem layout: no /bin/bash, no FHS
# /usr/bin/ paths, nix-ld enabled, nixpkgs bazel_8 (already patched for NixOS).
#
# Build:
#   nix-build image.nix
# Load:
#   docker load < result
# Run:
#   docker run --rm -it --network=host -v $PWD:/repo -w /repo ducktape-nixos-bazel bash
let
  nixpkgsSrc = fetchTarball "https://github.com/NixOS/nixpkgs/archive/nixos-unstable.tar.gz";
  pkgs = import nixpkgsSrc { system = "x86_64-linux"; };

  # Evaluate a real NixOS configuration (container mode)
  nixos = import "${nixpkgsSrc}/nixos/lib/eval-config.nix" {
    system = "x86_64-linux";
    modules = [
      (
        { config, pkgs, ... }:
        {
          boot.isContainer = true;

          # nix-ld: stub dynamic linker for Bazel-downloaded binaries
          programs.nix-ld.enable = true;

          environment.systemPackages = with pkgs; [
            bash
            coreutils
            findutils
            gnugrep
            gnused
            gawk
            diffutils
            gnutar
            gzip
            xz
            which
            file
            gcc
            gnumake
            binutils
            patchelf
            patch
            git
            curl
            cacert
            openssl
            python3
            direnv
            bazel_8
          ];

          nix.settings.experimental-features = [
            "nix-command"
            "flakes"
          ];

          networking.hostName = "bazel-test";
          networking.firewall.enable = false;
          users.users.root.shell = pkgs.bash;
          i18n.defaultLocale = "en_US.UTF-8";
          system.stateVersion = "25.11";
        }
      )
    ];
  };

  # NixOS system profile — all packages merged into /bin, /lib, etc.
  nixosPath = nixos.config.system.path;

  # nix-ld paths
  glibcLib = "${pkgs.glibc}/lib";
  gccLib = "${pkgs.stdenv.cc.cc.lib}/lib";
  nixLdPath = "${pkgs.glibc}/lib/ld-linux-x86-64.so.2";

  userBazelrc = pkgs.writeText "bazelrc" ''
    build --shell_executable=/bin/bash
    # PATH only for exec-config (host) tools. Not --action_env, which would
    # leak NixOS paths to RBE workers.
    build --host_action_env=PATH=/run/current-system/sw/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    # nix-ld env vars: host_action_env + repo_env only (local actions).
    build --host_action_env=NIX_LD
    build --host_action_env=NIX_LD_LIBRARY_PATH
    common --repo_env=NIX_LD
    common --repo_env=NIX_LD_LIBRARY_PATH

    try-import /root/.config/bazel/buildbuddy.bazelrc
  '';

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
