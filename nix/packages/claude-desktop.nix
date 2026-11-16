# Claude Desktop (GUI app) — Anthropic's Electron desktop client for Linux.
# Installed from the official .deb in Anthropic's apt repo (NOT Claude Code the
# CLI — that's a separate `programs.claude-code` module). The app ships its own
# Chromium/Electron runtime; we only supply the system shared libs it links
# against and patch the ELF RPATHs via autoPatchelfHook.
#
# To update (the apt repo is the source of truth, not a GitHub release):
#   curl -sSL https://downloads.claude.ai/claude-desktop/apt/stable/dists/stable/main/binary-amd64/Packages \
#     | awk 'BEGIN{RS=""} /Version:/{print}' | tail -1
#   # then bump `version`, re-fetch the .deb, and recompute the SRI hash:
#   nix hash to-sri --type sha256 "$(curl -sSL <deb-url> | sha256sum | cut -d' ' -f1)"
# Install docs: https://code.claude.com/docs/en/desktop-linux
{ lib, pkgs }:
let
  version = "4.23118.0";
in
pkgs.callPackage ./mk-electron-deb.nix { } {
  pname = "claude-desktop";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://downloads.claude.ai/claude-desktop/apt/stable/pool/main/c/claude-desktop/claude-desktop_${version}_amd64.deb";
    hash = "sha256-GtmW6MqrBcmzdGRgUcQMtrudhPNxdW8VuNNyiJdRWpc=";
  };

  desktopFile = "com.anthropic.Claude.desktop";
  desktopExec = "claude-desktop ";

  # Electron safeStorage dlopens libsecret to persist sign-in via the keyring.
  extraBuildInputs = with pkgs; [
    libnotify
    libsecret
    libseccomp
    libcap_ng
    pipewire
    util-linux
    libxtst
  ];

  libraryPath = with pkgs; [
    libGL
    libdrm
    libsecret
    libxkbcommon
    mesa
    libx11
    libxtst
    libxcb
  ];

  # Cowork locates QEMU here; the NixOS module supplies firmware and virtiofsd.
  path = with pkgs; [
    qemu_kvm
  ];

  meta = {
    description = "Claude Desktop — Anthropic's Electron desktop app (Linux beta)";
    homepage = "https://code.claude.com/docs/en/desktop-linux";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "claude-desktop";
  };
}
