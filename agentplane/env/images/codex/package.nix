# The VM and the independently published fixture image use the same executable.
{ pkgs, pkgsUnstable }:
let
  codex = import ./. { inherit (pkgsUnstable) codex; };
  voiceRuntime = with pkgsUnstable.gst_all_1; [
    gstreamer
    gst-plugins-base
    gst-plugins-good
  ];
in
pkgs.runCommand "codex-${codex.version}-without-voice"
  {
    nativeBuildInputs = [
      pkgs.binutils
      pkgs.removeReferencesTo
    ];
  }
  ''
    cp -a ${codex} $out
    chmod -R u+w $out
    rm -r $out/codex-resources/voice
    strip --strip-all $out/bin/*
    # These embedded voice-loader paths otherwise retain the unused media closure.
    remove-references-to \
      ${pkgs.lib.concatMapStringsSep " " (package: "-t ${pkgs.lib.getLib package}") voiceRuntime} \
      $out/bin/codex
  ''
