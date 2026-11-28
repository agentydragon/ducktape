{ pkgsUnstable }:

let
  inherit (pkgsUnstable) leanPackages;
in
leanPackages.buildLakePackage {
  pname = "lean-fmt";
  version = "4.35.0";
  src = pkgsUnstable.fetchurl {
    name = "lean-fmt-4.35.0.tar.gz";
    url = "https://codeload.github.com/jcreinhold/lean-fmt/tar.gz/refs/tags/v4.35.0";
    hash = "sha256-3Su/uE0Put7AgZzoZ4kn8vl1QP+7VZdfU1wVp99BRRE=";
  };
  sourceRoot = "lean-fmt-4.35.0";
  buildTargets = [ "lean-fmt" ];
  isLibrary = false;

  nativeBuildInputs = [ pkgsUnstable.makeWrapper ];
  postInstall = ''
    wrapProgram "$out/bin/lean-fmt" \
      --set LEAN_SYSROOT "${leanPackages.lean4}"
  '';

  meta = {
    description = "Lean 4 formatter and linter";
    homepage = "https://github.com/jcreinhold/lean-fmt";
    license = pkgsUnstable.lib.licenses.asl20;
    mainProgram = "lean-fmt";
    platforms = pkgsUnstable.lib.platforms.linux;
  };
}
