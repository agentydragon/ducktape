# The single static runner executable consumed by the OCI image and NixOS guest.
# The existing artifact pin remains the old wheel until the first Go binary release is
# published and sync-pins updates it. Reject that stale asset clearly during the cutover.
{
  pkgs,
  binary,
}:
pkgs.stdenvNoCC.mkDerivation {
  pname = "agentplane-runner";
  version = "released";
  dontUnpack = true;
  dontBuild = true;
  dontConfigure = true;
  dontCheck = true;

  preInstall = ''
    magic="$(head -c 4 ${binary} | od -An -tx1 | tr -d '[:space:]')"
    if [ "$magic" != "7f454c46" ]; then
      echo "agentplane-runner artifact is not an ELF binary; wait for the Go release pin to sync" >&2
      exit 1
    fi
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 ${binary} $out/bin/agentplane-runner
    ln -s agentplane-runner $out/bin/agentplane-runner-vm
    runHook postInstall
  '';

  meta = {
    mainProgram = "agentplane-runner";
    platforms = [ "x86_64-linux" ];
  };
}
