{
  artifacts,
  lib,
  pkgs,
}:
pkgs.stdenv.mkDerivation {
  pname = "bb";
  version = "5.0.339";
  src = artifacts.bb;
  dontUnpack = true;
  nativeBuildInputs = [ pkgs.makeWrapper ];
  installPhase = ''
    mkdir -p $out/bin
    cp $src $out/bin/bb
    chmod +x $out/bin/bb
    mv $out/bin/bb $out/bin/.bb-real
    makeWrapper $out/bin/.bb-real $out/bin/bb \
      --prefix PATH : ${lib.makeBinPath [ pkgs.file ]}
  '';
  meta = {
    description = "BuildBuddy CLI (remote bazel, etc.)";
    homepage = "https://github.com/buildbuddy-io/bazel";
    license = lib.licenses.mit;
    mainProgram = "bb";
    platforms = [ "x86_64-linux" ];
  };
}
