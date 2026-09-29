# Intel IPU7 (Lunar Lake) webcam support
#
# Requires kernel 6.17+ (IPU7 driver mainlined in 6.17).
# Uses the in-tree kernel driver with libcamera SoftISP pipeline.
#
# Hardware: Intel Core Ultra (Lunar Lake) with IPU7 (PCI 8086:645d)
# Sensors: OmniVision OV08X40 (rear), OVTI00AB (front)
# Dependencies: IVSC (Intel Visual Sensing Controller) for camera power gating
#
# Camera access paths:
# - PipeWire-native apps (Chrome with WebRtcPipeWireCamera, GNOME Snapshot):
#   use the libcamera PipeWire source directly via the camera portal.
# - V4L2-only apps (Zoom): need v4l2loopback bridge. See nix/debug/rugged/hw/webcam.md.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.ipu7Camera;
in
{
  options.ducktape.ipu7Camera = {
    enable = lib.mkEnableOption "Intel IPU7 (Lunar Lake) webcam support";
  };

  config = lib.mkIf cfg.enable {
    # Follow `linuxPackages_latest`; the current locked alias resolves to the same
    # Linux 7.2.7 derivation validated on `rugged` on 2026-09-28. The Cilium
    # FnSetRetval probe failure was fixed in Cilium 1.19.8, so the old kernel ceiling
    # no longer applies. The incident record is
    # `cluster/docs/lessons_learned/2026_07_16_cilium_set_retval_probe_kernel_7_2.md`.
    #
    #   >= 6.17   IPU7 camera driver mainlined in 6.17.
    #   >= 7.1.8  drm/xe TTM `beneficial_order` fix `ba7fd1634228`; without it this
    #             host hits a kswapd/Xe-shrinker swap storm. Confirmed present in
    #             7.1.8 by reverse-patch test (<../../../../nix/debug/rugged/stalls/report.md>).
    boot.kernelPackages = pkgs.linuxPackages_latest;

    # Firmware for IPU and Intel Visual Sensing Controller
    hardware.firmware = with pkgs; [
      ipu6-camera-bins
      ivsc-firmware
    ];

    # udev rules for camera device access
    services.udev.extraRules = ''
      SUBSYSTEM=="intel-ipu7-psys", MODE="0660", GROUP="video"
    '';

    # Userspace camera stack
    environment.systemPackages = with pkgs; [
      libcamera
    ];
  };
}
