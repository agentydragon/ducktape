# ChatGPT desktop app for Linux — OpenAI's Electron client with Codex.
# Installed from the official versioned .deb published for Ubuntu/Debian.
#
# The documented `latest/` URL is mutable, so keep the version and hash pinned
# to the versioned pool artifact. Update both from OpenAI's APT Packages index:
# https://persistent.oaistatic.com/codex-app-prod/linux/deb/dists/stable/main/binary-amd64/Packages
{ lib, pkgs }:
let
  version = "26.1103.60933";
in
pkgs.callPackage ./mk-electron-deb.nix { } {
  pname = "chatgpt";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://persistent.oaistatic.com/codex-app-prod/linux/deb/pool/main/c/chatgpt/chatgpt_${version}_amd64.deb";
    hash = "sha256-B0bpuldRgvXb4yYXuCpkXrk61XyliOWpNYaVprZa7nE=";
  };

  binary = "codex-launcher";
  desktopExec = "chatgpt ";
  dataArchive = "data.tar.xz";

  # Electron safeStorage dlopens libsecret to persist sign-in via the keyring.
  extraBuildInputs = with pkgs; [
    gdk-pixbuf
    libnotify
    libsecret
    libusb1
    tpm2-tss
  ];

  libraryPath = with pkgs; [
    libGL
    libdrm
    libsecret
    libxkbcommon
    mesa
    libx11
    libxcb
  ];

  # The launcher needs its sandbox, project and desktop integration tools.
  path = with pkgs; [
    bubblewrap
    git
    glib
    xdg-utils
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

  meta = {
    description = "ChatGPT desktop app for Linux with Codex";
    homepage = "https://developers.openai.com/codex/app";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "chatgpt";
  };
}
