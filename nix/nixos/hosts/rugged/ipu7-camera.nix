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
# - V4L2-only apps (Zoom): need v4l2loopback bridge. See debug/rugged/hw/webcam.md.
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
    # Pinned to the 7.1 *series* — the intersection of three independent bounds.
    # `linuxPackages_latest` is not a substitute: it is a moving alias, and on
    # 2026-09-04 it floated across the upper bound and took this host's CNI down.
    #
    #   >= 6.17   IPU7 camera driver mainlined in 6.17.
    #   >= 7.1.8  drm/xe TTM `beneficial_order` fix `ba7fd1634228`; without it this
    #             host hits a kswapd/Xe-shrinker swap storm. Confirmed present in
    #             7.1.8 by reverse-patch test (<../../../../debug/rugged/stalls/report.md>).
    #   <  7.2    Kernel `b1f7f67b74c2` (first in 7.2-rc1) hardens the verifier, so
    #             cilium/ebpf's FnSetRetval probe gets EINVAL and cilium-agent
    #             fatals at startup — observed on 7.2.0 with Cilium 1.19.6
    #             (<../../../../cluster/docs/lessons_learned/2026_07_16_cilium_set_retval_probe_kernel_7_2.md>).
    #
    # A floor plus a ceiling is what a series attribute expresses and an alias
    # cannot. 7.1 is not LTS: when it leaves nixpkgs, re-derive the intersection
    # rather than reaching for `latest` again. 6.18 (LTS, and what the other nodes
    # run) qualifies if `ba7fd1634228` reached its stable series — untested.
    boot.kernelPackages = pkgs.linuxPackages_7_1;

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
