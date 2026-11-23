{
  pkgs,
  bb,
  bbapi,
}:
let
  # actions/checkout and upload-artifact use the runner's mounted Node binary.
  substrate = import ../../nix/lib/nix-ld-image.nix { inherit pkgs; };
  root = pkgs.buildEnv {
    name = "github-actions-ci-tools";
    paths = [
      bb
      bbapi
      substrate.nixLdLibraries
      pkgs.coreutils
      pkgs.dockerTools.caCertificates
      pkgs.dockerTools.fakeNss
      pkgs.gawk
      pkgs.gitMinimal
      pkgs.gnugrep
      pkgs.gnused
      pkgs.gnutar
      pkgs.gzip
      pkgs.jq
      pkgs.nix
      pkgs.python3
      pkgs.util-linux
    ];
    pathsToLink = [
      "/bin"
      "/etc"
      "/share/nix-ld"
    ];
  };
in
pkgs.dockerTools.buildLayeredImage {
  name = "github-actions-ci";
  tag = null;
  contents = [ root ];
  includeNixDB = true;
  maxLayers = 4;
  fakeRootCommands = ''
    ${substrate.fakeRootCommands}
    mkdir -p etc/nix root
    printf 'experimental-features = nix-command flakes\nbuild-users-group =\n' > etc/nix/nix.conf
  '';
  config = {
    Env = [
      "PATH=/bin:/usr/bin"
      "HOME=/root"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
    ]
    ++ substrate.env;
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
  };
}
