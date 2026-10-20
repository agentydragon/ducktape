# The released static `agentplane` command (the `agentplane-cli` pin in nix/artifact-pins.json),
# carried by the sandbox, build and runner images and the NixOS guest.
{
  pkgs,
  binary,
}:
pkgs.stdenvNoCC.mkDerivation {
  pname = "agentplane-cli";
  version = "released";
  dontUnpack = true;
  dontBuild = true;
  dontConfigure = true;
  dontCheck = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 ${binary} $out/bin/agentplane
    runHook postInstall
  '';

  meta = {
    mainProgram = "agentplane";
    platforms = [ "x86_64-linux" ];
  };
}
