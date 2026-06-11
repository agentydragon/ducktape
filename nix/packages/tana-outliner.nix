# Tana: knowledge graph / note-taking desktop app (Electron)
# Installed from .deb via GitHub Releases (tanainc/tana-desktop-releases)
#
# To update:
#   nix run nixpkgs#nix-update -- --flake tana-outliner
{
  lib,
  pkgs,
}:
let
  version = "1.520.8";
in
pkgs.stdenv.mkDerivation {
  pname = "tana-outliner";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://github.com/tanainc/tana-desktop-releases/releases/download/v${version}/tana-outliner_${version}_amd64.deb";
    hash = "sha256-StDrc9+Nce3q/4Z4xqYn4pT/ZgQ6y9Qy+9OqkCaruOk=";
  };

  nativeBuildInputs = with pkgs; [
    autoPatchelfHook
    makeWrapper
  ];

  buildInputs = with pkgs; [
    alsa-lib
    at-spi2-atk
    at-spi2-core
    cairo
    cups
    dbus
    expat
    glib
    gtk3
    libdrm
    libGL
    libxkbcommon
    mesa
    nspr
    nss
    pango
    xorg.libX11
    xorg.libXcomposite
    xorg.libXdamage
    xorg.libXext
    xorg.libXfixes
    xorg.libXrandr
    xorg.libxcb
  ];

  runtimeDependencies = with pkgs; [
    (lib.getLib systemd)
  ];

  dontBuild = true;
  dontConfigure = true;

  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    # Extract .deb using bsdtar (handles SUID chrome-sandbox without errors)
    ${pkgs.libarchive}/bin/bsdtar -xf $src --no-same-permissions
    ${pkgs.libarchive}/bin/bsdtar -xf data.tar.* --no-same-permissions

    # Install everything from usr/
    mkdir -p $out/lib
    cp -r usr/share $out/share
    cp -r usr/lib/tana-outliner $out/lib/tana-outliner

    # Create wrapper binary
    mkdir -p $out/bin
    makeWrapper $out/lib/tana-outliner/tana-outliner $out/bin/tana-outliner \
      --prefix LD_LIBRARY_PATH : "${
        lib.makeLibraryPath (
          with pkgs;
          [
            libGL
            libxkbcommon
            mesa
            xorg.libX11
            xorg.libxcb
          ]
        )
      }"

    # Fix desktop entry to use full path
    substituteInPlace $out/share/applications/tana-outliner.desktop \
      --replace-fail "Exec=tana-outliner" "Exec=$out/bin/tana-outliner"

    runHook postInstall
  '';

  meta = {
    description = "Tana - knowledge graph for you & your team";
    homepage = "https://tana.inc";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "tana-outliner";
  };
}
