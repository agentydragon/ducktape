# Claude Desktop "Cowork" runs agent tools in a local QEMU microVM (smol-bin
# image + a virtiofsd bundled inside the app). Before starting a VM it gates on
# a presence check that probes three hard-coded Debian paths:
#
#   qemuPath       -> `qemu-system-x86_64` on PATH
#   firmwarePath   -> /usr/share/OVMF/OVMF_CODE_4M.fd, then OVMF_CODE.fd
#   virtiofsdPath  -> /usr/libexec/virtiofsd, then /usr/bin/virtiofsd
#
# None of those resolve on NixOS: /usr/share and /usr/libexec aren't populated
# (only /usr/bin is, via a read-only envfs mount), nixpkgs ships no standalone
# virtiofsd, and the detection list does NOT include the virtiofsd bundled with
# the app. So without help Cowork reports "Virtualization isn't fully set up"
# and refuses to start. The detection source lives in app.asar (minified): the
# firmware probe list `ogi`, the virtiofsd list `sgi`, and a VARS-template
# derivation `Qgi = p => p.replace("OVMF_CODE","OVMF_VARS")`.
#
# This module satisfies the firmware + virtiofsd checks by symlinking those
# exact /usr paths into the nix store via tmpfiles. qemu-system-x86_64 is put on
# PATH by the claude-desktop package wrapper (see packages/claude-desktop.nix).
# /dev/kvm access is host-dependent; on hosts where it is 0666 (default NixOS
# GNOME) no group setup is needed.
{
  config,
  pkgs,
  lib,
  ...
}:
let
  # Same callPackage the home-manager package set uses, so this resolves to the
  # identical store path the user runs — and thus to its bundled virtiofsd.
  claudeDesktop = pkgs.callPackage ../../packages/claude-desktop.nix { };
  ovmf = pkgs.OVMF;
in
{
  options.ducktape.cowork = {
    enable = lib.mkEnableOption "Claude Desktop Cowork microVM support (OVMF firmware + bundled virtiofsd at the Debian /usr paths the app probes)";
  };

  config = lib.mkIf config.ducktape.cowork.enable {
    systemd.tmpfiles.rules = [
      "d /usr/libexec 0755 root root -"
      "L+ /usr/libexec/virtiofsd - - - - ${claudeDesktop}/lib/claude-desktop/resources/virtiofsd"

      "d /usr/share/OVMF 0755 root root -"
      # The probe tries OVMF_CODE_4M.fd first and falls back to OVMF_CODE.fd;
      # nixpkgs only ships the 2MB OVMF_CODE.fd, which boots the smol-bin guest.
      "L+ /usr/share/OVMF/OVMF_CODE.fd - - - - ${ovmf.fd}/FV/OVMF_CODE.fd"
      # Qgi derives the VARS template from the CODE path by CODE->VARS replace.
      "L+ /usr/share/OVMF/OVMF_VARS.fd - - - - ${ovmf.fd}/FV/OVMF_VARS.fd"
    ];
  };
}
