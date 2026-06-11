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
    # IPU7 mainlined in 6.17; latest kernel includes it.
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
