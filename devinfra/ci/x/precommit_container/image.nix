# Experimental GitHub job container. Build with build.sh; not a production pin.
let
  flake = builtins.getFlake (toString ../../../..);
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
  precommit = flake.packages.x86_64-linux.precommit;
  base = builtins.storePath (builtins.getEnv "DUCKTAPE_PRECOMMIT_BASE_IMAGE");
  identity = pkgs.writeTextDir "etc/ducktape-precommit-closure" "${precommit}\n";
in
pkgs.dockerTools.buildLayeredImage {
  name = "ducktape-precommit-spike";
  tag = "candidate";
  fromImage = base;
  contents = [ identity ];
  includeStorePaths = true;
  architecture = "amd64";
  created = "1970-01-01T00:00:01Z";
  mtime = "1970-01-01T00:00:01Z";
  config = {
    # Ubuntu supplies the dynamic loader/libraries required by GitHub's mounted
    # Node runtime. Hooks and helper commands come from the same locked Nix graph.
    Env = [
      "PATH=${
        pkgs.lib.makeBinPath [
          precommit
          pkgs.git
          pkgs.python3
        ]
      }:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "GIT_SSL_CAINFO=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    ];
    Cmd = [ "/bin/bash" ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
  };
}
