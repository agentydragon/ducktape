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
    pkgs.gobject-introspection
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
    appDir="$NIX_BUILD_TOP/haku-approvals"
    mkdir -p "$appDir"
    unzip -q "$src" -d "$appDir"
    install -Dm644 "$appDir/haku-approvals.js" "$out/libexec/haku-approvals.js"
    install -Dm644 "$appDir/logo.svg" "$out/libexec/logo.svg"
    install -Dm644 "$appDir/logo.svg" "$out/share/icons/hicolor/scalable/apps/haku-approvals.svg"
    install -Dm644 "$appDir/haku-approvals.desktop" "$out/share/applications/haku-approvals.desktop"

    makeWrapper ${pkgs.gjs}/bin/gjs "$out/bin/haku-approvals" \
      --add-flags "-m" \
      --add-flags "$out/libexec/haku-approvals.js" \
      --set-default WEBKIT_DISABLE_DMABUF_RENDERER 1
  '';

  meta = {
    description = "Haku Console approvals GTK/WebKit application";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "haku-approvals";
    platforms = [ "x86_64-linux" ];
  };
}
