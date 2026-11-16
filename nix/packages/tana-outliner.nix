# Tana: knowledge graph / note-taking desktop app (Electron)
# Installed from .deb via GitHub Releases (tanainc/tana-desktop-releases)
#
# To update:
#   nix run nixpkgs#nix-update -- --flake tana-outliner
{ lib, pkgs }:
let
  version = "1.531.0";
in
pkgs.callPackage ./mk-electron-deb.nix { } {
  pname = "tana-outliner";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://github.com/tanainc/tana-desktop-releases/releases/download/v${version}/tana-outliner_${version}_amd64.deb";
    hash = "sha256-FV/iOkQLuCZHUVdO002426xRpu1tDif4jLH0ASIFS8I=";
  };

  libraryPath = with pkgs; [
    libGL
    libxkbcommon
    mesa
    libx11
    libxcb
  ];

  meta = {
    description = "Tana - knowledge graph for you & your team";
    homepage = "https://tana.inc";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "tana-outliner";
  };
}
