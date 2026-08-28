# ChatGPT desktop app for Linux — OpenAI's Electron client with Codex.
# Installed from the official versioned .deb published for Ubuntu/Debian.
#
# The documented `latest/` URL is mutable, so keep the version and hash pinned
# to the versioned pool artifact. Update both from OpenAI's APT Packages index:
# https://persistent.oaistatic.com/codex-app-prod/linux/deb/dists/stable/main/binary-amd64/Packages
{
  lib,
  pkgs,
}:
let
  version = "26.825.31414";
in
pkgs.stdenv.mkDerivation {
  pname = "chatgpt";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://persistent.oaistatic.com/codex-app-prod/linux/deb/pool/main/c/chatgpt/chatgpt_${version}_amd64.deb";
    hash = "sha256-wXMEi6gPevnNiQT5ofJyr/SUejFPb+l9obuDaEds3Pk=";
  };

  nativeBuildInputs = with pkgs; [
    autoPatchelfHook
    makeWrapper
  ];

  # These are the shared libraries named by the bundled Electron runtime and
  # native modules. libsecret is dlopen'd by Electron's safeStorage backend so
  # GNOME Keyring can persist the app's sign-in state.
  buildInputs = with pkgs; [
    alsa-lib
    at-spi2-atk
    at-spi2-core
    cairo
    cups
    dbus
    expat
    gdk-pixbuf
    glib
    gtk3
    libdrm
    libGL
    libnotify
    libsecret
    libusb1
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
  ];

  # The universal bundle includes optional Qt shims without their Qt runtime,
  # plus musl prebuilds alongside the glibc native modules. Debian does not
  # depend on Qt, and glibc hosts never select the musl variants.
  autoPatchelfIgnoreMissingDeps = [
    "libQt5Core.so.5"
    "libQt5Gui.so.5"
    "libQt5Widgets.so.5"
    "libQt6Core.so.6"
    "libQt6Gui.so.6"
    "libQt6Widgets.so.6"
    "libc.musl-x86_64.so.1"
  ];

  runtimeDependencies = [ (lib.getLib pkgs.systemd) ];

  dontBuild = true;
  dontConfigure = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    # Extract the .deb without preserving Debian ownership or setuid bits.
    ${pkgs.libarchive}/bin/bsdtar -xf $src --no-same-permissions
    ${pkgs.libarchive}/bin/bsdtar -xf data.tar.xz --no-same-permissions

    mkdir -p $out/lib
    cp -r usr/share $out/share
    cp -r usr/lib/chatgpt $out/lib/chatgpt

    # The app invokes bwrap for its Linux sandbox, xdg-open for links/files,
    # and git for project integration; Debian would normally provide these via
    # package dependencies or the host PATH.
    mkdir -p $out/bin
    makeWrapper $out/lib/chatgpt/codex-launcher $out/bin/chatgpt \
      --prefix LD_LIBRARY_PATH : "${
        lib.makeLibraryPath (
          with pkgs;
          [
            libGL
            libdrm
            libsecret
            libxkbcommon
            mesa
            libx11
            libxcb
          ]
        )
      }" \
      --prefix PATH : "${
        lib.makeBinPath [
          pkgs.bubblewrap
          pkgs.git
          pkgs.glib
          pkgs.xdg-utils
        ]
      }"

    substituteInPlace $out/share/applications/chatgpt.desktop \
      --replace-fail "Exec=chatgpt " "Exec=$out/bin/chatgpt "

    runHook postInstall
  '';

  meta = {
    description = "ChatGPT desktop app for Linux with Codex";
    homepage = "https://developers.openai.com/codex/app";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "chatgpt";
  };
}
