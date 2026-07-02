# bazel-diff — Merkle-hash based Bazel target diff.
# https://github.com/Tinder/bazel-diff
#
# Distributed as a single fat JAR. Wraps it in a `bazel-diff` shell entry
# that forwards to `java -jar`, so consumers can `bazel-diff generate-hashes …`
# without knowing where the jar lives.
{
  pkgs,
  lib,
}:
let
  version = "16.0.0";
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "bazel-diff";
  inherit version;
  src = pkgs.fetchurl {
    url = "https://github.com/Tinder/bazel-diff/releases/download/${version}/bazel-diff_deploy.jar";
    hash = "sha256-wtZvqabI5l9YqZ08ZYmThONg+LXXXywq/148s6rBTJQ=";
  };
  dontUnpack = true;
  nativeBuildInputs = [ pkgs.makeWrapper ];
  installPhase = ''
    install -Dm644 $src $out/share/java/bazel-diff.jar
    makeWrapper ${pkgs.jre_headless}/bin/java $out/bin/bazel-diff \
      --add-flags "-jar $out/share/java/bazel-diff.jar"
  '';
  meta = {
    description = "Merkle-hash based Bazel target diff (query, not cquery)";
    homepage = "https://github.com/Tinder/bazel-diff";
    license = lib.licenses.bsd3;
    mainProgram = "bazel-diff";
    platforms = lib.platforms.linux;
  };
}
