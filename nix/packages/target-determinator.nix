# target-determinator — pre-built binary from GitHub releases.
# https://github.com/bazel-contrib/target-determinator
{
  pkgs,
  lib,
}:
let
  version = "0.34.0";
in
pkgs.stdenv.mkDerivation {
  pname = "target-determinator";
  inherit version;
  src = pkgs.fetchurl {
    url = "https://github.com/bazel-contrib/target-determinator/releases/download/v${version}/target-determinator.linux.amd64";
    hash = "sha256-EV4cY9OeLNDQsBHJ+tyA8FnwIRdqSuDeIjLN2DsfgBE=";
  };
  dontUnpack = true;
  installPhase = ''
    install -Dm755 $src $out/bin/target-determinator
  '';
  meta = {
    description = "Determine which Bazel targets changed between two git commits";
    homepage = "https://github.com/bazel-contrib/target-determinator";
    license = lib.licenses.asl20;
    mainProgram = "target-determinator";
    platforms = [ "x86_64-linux" ];
  };
}
