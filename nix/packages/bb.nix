{
  artifacts,
  lib,
  pkgs,
}:
pkgs.stdenv.mkDerivation {
  pname = "bb";
  version = "5.0.445";
  src = artifacts.bb;
  dontUnpack = true;
  installPhase = ''
    mkdir -p $out/bin
    cp $src $out/bin/bb
    chmod +x $out/bin/bb
  '';
  meta = {
    description = "BuildBuddy CLI (remote bazel, etc.)";
    homepage = "https://github.com/buildbuddy-io/bazel";
    license = lib.licenses.mit;
    mainProgram = "bb";
    platforms = [ "x86_64-linux" ];
  };
}
