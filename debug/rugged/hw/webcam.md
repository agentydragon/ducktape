# Webcam — IPU7 + OmniVision OV08X40

**Goal**: Painless Zoom / Chrome / browser video calls.

**Working path**: libcamera + SoftISP + PipeWire camera portal.

- NixOS module: <nix/nixos/hosts/rugged/ipu7-camera.nix> (enabled)
- `snapshot` works (with `GSK_RENDERER=gl`).
- PipeWire camera portal (`org.freedesktop.portal.Camera`) is active and
  `IsCameraPresent=true`. WirePlumber exposes PipeWire node 97 ("Built-in Front
  Camera") via `api.libcamera.source`.
- **Chrome 147 works** with: `NIXOS_OZONE_WL=1` (native Wayland, not XWayland)
  - `--enable-features=WebRtcPipeWireCamera`. Without these, Chrome falls back to
    raw V4L2 `/dev/video*` nodes which are non-functional (IPU7 needs libcamera ISP).
    The feature flag is `WebRtcPipeWireCamera` (not `PipeWireCamera`).
- Known issue: green tint from uncalibrated sensor. Needs color correction matrix
  in `/usr/share/libcamera/ipa/simple/uncalibrated.yaml`.
- **Zoom 6.6**: uses raw V4L2 only for camera (PipeWire support is screen-sharing only).
  Shows "ipu7" (raw V4L2 node), all black. Confirmed by `strings` on binary: camera uses
  `/dev/video%u`, no `AccessCamera` portal calls in Zoom's own code (only in embedded
  Chromium `libcef.so`). No indication Zoom is working on PipeWire camera support.

**Next steps**:

- Test if green tint fix resolves color issues

## v4l2loopback recipe for Zoom

Not deployed. Use if Zoom camera is needed later.

Add to `ipu7-camera.nix`:

```nix
# v4l2loopback kernel module
boot.extraModulePackages = [ config.boot.kernelPackages.v4l2loopback ];
boot.kernelModules = [ "v4l2loopback" ];
boot.extraModprobeConfig = ''
  options v4l2loopback video_nr=99 card_label="IPU7 Camera" exclusive_caps=1
'';

# GStreamer for the bridge pipeline
environment.systemPackages = with pkgs; [
  gst_all_1.gstreamer
  gst_all_1.gst-plugins-base
  gst_all_1.gst-plugins-good
];
```

Then bridge PipeWire camera → loopback (run before starting Zoom):

```bash
# Find the libcamera PipeWire node ID (look for media.role = "Camera")
NODE=$(pw-cli list-objects | grep -B5 'media.role = "Camera"' | grep 'id ' | awk '{print $2}' | tr -d ',')

# Set GStreamer plugin path (NixOS — adjust store paths)
export GST_PLUGIN_PATH="$(nix-build '<nixpkgs>' -A pipewire --no-out-link)/lib/gstreamer-1.0:$(nix-build '<nixpkgs>' -A gst_all_1.gst-plugins-good --no-out-link)/lib/gstreamer-1.0:$(nix-build '<nixpkgs>' -A gst_all_1.gst-plugins-base --no-out-link)/lib/gstreamer-1.0:$(nix-build '<nixpkgs>' -A gst_all_1.gstreamer --no-out-link)/lib/gstreamer-1.0"

# Bridge: PipeWire camera → v4l2loopback
gst-launch-1.0 -e pipewiresrc path=$NODE ! videoconvert ! \
  video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1 ! \
  v4l2sink device=/dev/video99
```

Can also be wired as a systemd user service (see git history for a prior version).

## Vulkan crash

GTK4 apps (including `snapshot`) segfault with `VK_ERROR_DEVICE_LOST`
on Lunar Lake. Workaround: `GSK_RENDERER=gl`. TODO in `default.nix` to add to
`environment.sessionVariables`.
