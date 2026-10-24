{
  lib,
  stdenvNoCC,
}:
let
  extensionUuid = "center-status-area@allegedly.works";
in
stdenvNoCC.mkDerivation {
  pname = "gnome-shell-center-status-area";
  version = "0.1.0";
  src = ./gnome_shell_center_status_area;
  dontUnpack = true;
  dontBuild = true;

  installPhase = ''
    extensionDir="$out/share/gnome-shell/extensions/${extensionUuid}"
    install -d "$extensionDir"
    install -m644 "$src/extension.js" "$extensionDir/extension.js"
    install -m644 "$src/metadata.json" "$extensionDir/metadata.json"
  '';

  passthru.extensionUuid = extensionUuid;

  meta = {
    description = "Move GNOME Shell status indicators into the center panel box";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    platforms = lib.platforms.linux;
  };
}
