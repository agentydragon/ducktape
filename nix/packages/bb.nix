{
  artifacts,
  lib,
  pkgs,
}:
pkgs.stdenv.mkDerivation {
  pname = "bb";
  # CLEANUP(added 2026-08-27): Our own build from third_party/bb — upstream
  #   cli-v5.0.387 + buildbuddy#13067 (binary-deletion patchset fix). Revert
  #   the version and the artifact-pins.json url to the stock
  #   buildbuddy-io/bazel release, and delete third_party/bb +
  #   .github/workflows/bb-patched.yml, once a bb CLI release carries #13067.
  version = "5.0.387-pr13067";
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
