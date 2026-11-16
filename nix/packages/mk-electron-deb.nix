{ lib, pkgs }:
{
  pname,
  version,
  src,
  meta,
  libraryPath,
  extraBuildInputs ? [ ],
  path ? [ ],
  binary ? pname,
  desktopFile ? "${pname}.desktop",
  desktopExec ? pname,
  dataArchive ? "data.tar.*",
  autoPatchelfIgnoreMissingDeps ? [ ],
}:
pkgs.stdenv.mkDerivation {
  inherit
    pname
    version
    src
    meta
    autoPatchelfIgnoreMissingDeps
    ;

  nativeBuildInputs = with pkgs; [
    autoPatchelfHook
    makeWrapper
  ];

  buildInputs =
    with pkgs;
    [
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
      libx11
      libxcomposite
      libxdamage
      libxext
      libxfixes
      libxrandr
      libxcb
    ]
    ++ extraBuildInputs;

  runtimeDependencies = [ (lib.getLib pkgs.systemd) ];
  dontBuild = true;
  dontConfigure = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    # Preserve neither Debian ownership nor setuid permissions.
    ${pkgs.libarchive}/bin/bsdtar -xf $src --no-same-permissions
    ${pkgs.libarchive}/bin/bsdtar -xf ${dataArchive} --no-same-permissions

    mkdir -p $out/lib
    cp -r usr/share $out/share
    cp -r usr/lib/${pname} $out/lib/${pname}

    mkdir -p $out/bin
    makeWrapper $out/lib/${pname}/${binary} $out/bin/${pname} \
      --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath libraryPath}" ${
        lib.optionalString (path != [ ]) ''--prefix PATH : "${lib.makeBinPath path}"''
      }

    substituteInPlace $out/share/applications/${desktopFile} \
      --replace-fail "Exec=${desktopExec}" "Exec=$out/bin/${desktopExec}"

    runHook postInstall
  '';
}
