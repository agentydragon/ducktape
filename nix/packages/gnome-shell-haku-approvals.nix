{
  artifacts,
  lib,
  pkgs,
}:
pkgs.stdenvNoCC.mkDerivation {
  pname = "haku-approvals";
  version = "latest";
  src = artifacts.haku-approvals;
  dontUnpack = true;

  nativeBuildInputs = [
    pkgs.makeWrapper
    pkgs.unzip
    pkgs.wrapGAppsHook3
  ];
  buildInputs = [
    pkgs.gjs
    pkgs.gtk3
    pkgs.webkitgtk_4_1
  ];

  installPhase = ''
    uuid="haku-approvals@allegedly.works"
    extDir="$out/share/gnome-shell/extensions/$uuid"

    mkdir -p "$extDir"
    unzip -q "$src" -d "$extDir"
    install -Dm644 "$extDir/haku-approvals-window.js" "$out/libexec/haku-approvals-window.js"
    install -Dm644 "$extDir/logo.svg" "$out/libexec/logo.svg"
    install -Dm644 "$extDir/logo.svg" "$out/share/icons/hicolor/scalable/apps/haku-approvals.svg"
    install -Dm644 "$extDir/haku-approvals.desktop" "$out/share/applications/haku-approvals.desktop"

    makeWrapper ${pkgs.gjs}/bin/gjs "$out/bin/haku-approvals" \
      --add-flags "-m" \
      --add-flags "$out/libexec/haku-approvals-window.js"
    ln -s ../../../../bin/haku-approvals "$extDir/haku-approvals"
  '';

  passthru.extensionUuid = "haku-approvals@allegedly.works";

  meta = {
    description = "Haku Console approvals window and GNOME Shell extension";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "haku-approvals";
    platforms = [ "x86_64-linux" ];
  };
}
