{ pkgs, atticServer }:
let
  binLinks = pkgs.runCommand "attic-bin-links" { } ''
    mkdir -p "$out/bin"
    ln -s ${atticServer}/bin/atticd "$out/bin/atticd"
    ln -s ${atticServer}/bin/atticadm "$out/bin/atticadm"
    ln -s ${pkgs.busybox}/bin/sh "$out/bin/sh"
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "attic";
  contents = [
    binLinks
    pkgs.cacert
  ];
  includeStorePaths = true;
  fakeRootCommands = ''
    mkdir -p tmp
    chmod 1777 tmp
  '';
  config = {
    User = "1000:1000";
    Env = [
      "PATH=/bin"
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-certificates.crt"
    ];
    Entrypoint = [ "/bin/atticd" ];
    Labels = {
      "org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
      "works.allegedly.attic-revision" = "295a33a1e4a5138868e6f45dc75ed68a05c0300f";
      "works.allegedly.nix-source" = "${atticServer}";
    };
  };
}
