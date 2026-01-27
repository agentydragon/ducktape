# SPICE Lag Investigation

## Problem Statement

SPICE is laggy when connecting from Atlas (Proxmox host) to wyrm (VM), even though they're on the same physical machine. This rules out network latency.

## Setup Context

- **Atlas**: Proxmox host with two GPUs passed through to wyrm
- **wyrm**: VM with GPU passthrough (NVIDIA 5090 mentioned), intended for compute only
- **SPICE proxy**: Configured via `atlas.agentydragon.com` in `/etc/pve/datacenter.cfg`

## Hypotheses

### H1: virtio-gl without host GPU (HIGH LIKELIHOOD)

VM config uses `vga=virtio-gl`, but both GPUs are passed through to the guest. VirGL requires a host GPU to work - without one, it falls back to software rendering (llvmpipe), which is CPU-intensive and laggy.

**Diagnostic**: Check `glxinfo | grep renderer` on wyrm - if it shows `llvmpipe`, this is confirmed.

### H2: Wrong display adapter type

The VM might be configured with a display adapter that doesn't match the installed guest drivers, causing software fallback.

**Diagnostic**: Compare VM config (`vga=` setting) with loaded kernel modules on guest.

### H3: QXL driver issues

If using QXL, the guest might not have the qxl kernel module loaded or xorg configured for it.

**Diagnostic**: Check `lsmod | grep qxl` and Xorg logs.

### H4: SPICE streaming/compression overhead

SPICE video streaming mode might be doing expensive software encoding even for local connections.

**Diagnostic**: Check VM's SPICE options in config, look for streaming-video settings.

### H5: spice-vdagent not running

The SPICE agent helps with display resizing and can affect performance.

**Diagnostic**: Check `systemctl status spice-vdagentd` on guest.

### H6: SPICE traffic routing through VPS even for local connections

The `spice_proxy: atlas.agentydragon.com` setting might cause SPICE clients to connect via the VPS nginx proxy even when on the same host.

**Diagnostic**: Check DNS resolution of `atlas.agentydragon.com` from Atlas, and verify active connections are local.

## Diagnostic Commands

### On wyrm (guest)

```bash
# Display devices visible to guest
lspci | grep -i vga

# Display drivers loaded
lsmod | grep -E 'qxl|virtio_gpu|bochs|cirrus|nvidia'

# Actual renderer (KEY diagnostic)
glxinfo | grep -i renderer

# SPICE agent status
systemctl status spice-vdagentd

# Xorg display driver
grep -i driver /var/log/Xorg.0.log 2>/dev/null || journalctl -b | grep -i 'graphics\|display\|gpu' | head -20
```

### On atlas (host)

```bash
# VM config for wyrm (assumed VM ID 100)
cat /etc/pve/qemu-server/*.conf | grep -E 'vga|display|spice|name'

# Host GPU status (should be empty if all passed through)
lspci | grep -i vga

# VFIO binding status
lspci -nnk | grep -A3 -i nvidia
```

## Findings

### 2026-01-26: Initial Diagnostics

**On wyrm (guest):**

```
$ lspci | grep -i vga
00:01.0 VGA compatible controller: Red Hat, Inc. QXL paravirtual graphic card (rev 05)
01:00.0 VGA compatible controller: NVIDIA Corporation Device 2b85 (rev a1)
02:00.0 VGA compatible controller: NVIDIA Corporation Device 2b85 (rev a1)

$ lsmod | grep -E 'qxl|virtio_gpu|nvidia'
nvidia_uvm           2146304  0
nvidia_drm            135168  7
nvidia_modeset       1744896  3 nvidia_drm
nvidia              14372864  64 nvidia_uvm,nvidia_modeset
qxl                    86016  1
...

$ glxinfo | grep renderer
OpenGL renderer string: llvmpipe (LLVM 15.0.7, 256 bits)
```

**On atlas (host):**

```
$ lspci | grep -i vga
01:00.0 VGA compatible controller: NVIDIA Corporation GB202 [GeForce RTX 5090] (rev a1)
03:00.0 VGA compatible controller: NVIDIA Corporation GB202 [GeForce RTX 5090] (rev a1)
7a:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Granite Ridge [Radeon Graphics] (rev c9)
```

**VM config (wyrm):**

```
name: wyrm
spice_enhancements: videostreaming=all
vga: qxl
audio0: device=ich9-intel-hda,driver=spice
```

### Analysis

**ROOT CAUSE IDENTIFIED: Software rendering via llvmpipe**

Despite QXL being configured and the qxl kernel module loaded, OpenGL rendering falls back to llvmpipe (CPU-based software rendering). This means every frame update is rendered on CPU, which is slow and laggy.

**Why is QXL not providing hardware acceleration?**

QXL is a paravirtualized 2D graphics adapter - it accelerates 2D operations but does NOT provide OpenGL/3D acceleration. For 3D, you need either:

1. **virtio-gpu with virgl** - requires host GPU (Atlas has AMD iGPU available)
2. **GPU passthrough** - but then SPICE can't see the output
3. **Accept software rendering** - current state, causes lag

**Additional factor: `spice_enhancements: videostreaming=all`**

This setting encodes ALL screen updates as video streams, adding encoding overhead on top of the already-slow software rendering.

### Key Insight

The NVIDIAs are passed through for compute (correct), but the display stack is:

- QXL virtual GPU → qxl kernel module → Mesa → **llvmpipe (software)**

QXL only accelerates 2D. Any 3D/compositing (like GNOME Shell, Firefox, etc.) hits llvmpipe.

### H6 Ruled Out: Local connections stay local

```
$ ssh root@atlas 'host atlas.agentydragon.com'
atlas.agentydragon.com has address 100.64.1.30  # Atlas's own Tailscale IP

$ ssh root@atlas 'ss -tn state established | grep 3128'
[::1]:59368  [::1]:3128   # All connections are localhost
[::1]:3128   [::1]:59420
...
```

When connecting from Atlas, `atlas.agentydragon.com` resolves to Atlas's Tailscale IP (100.64.1.30), and connections stay on localhost. VPS proxy is not involved for local SPICE sessions.

**Note:** Remote connections (from laptops etc.) would go through VPS, but that's not causing the local lag.

## Potential Solutions

### Option 1: Switch to virtio-gpu with VirGL (Recommended)

Atlas has an AMD iGPU (Radeon Graphics) not passed through. Change VM config:

```
vga: virtio-gl
```

This enables VirGL, which forwards OpenGL calls to the host's AMD iGPU. Should provide hardware-accelerated 3D rendering over SPICE.

**Pros:** Hardware acceleration, keeps SPICE working
**Cons:** Requires guest driver support, may need config tweaks

### Option 2: Disable desktop compositing

If using GNOME, switch to Xorg session (not Wayland) and disable compositing, or use a lighter WM that doesn't require 3D.

**Pros:** No VM config changes needed
**Cons:** Degraded desktop experience

### Option 3: Tune SPICE settings

Remove or change `spice_enhancements: videostreaming=all` to reduce encoding overhead:

```
spice_enhancements: videostreaming=off
```

**Pros:** Reduces CPU overhead
**Cons:** Doesn't fix the underlying software rendering issue

### Option 4: Use one NVIDIA for display via Looking Glass

Pass through one GPU but use Looking Glass to capture its framebuffer and display via shared memory. Very low latency.

**Pros:** Full GPU acceleration, low latency
**Cons:** Complex setup, dedicates GPU to display

## Measurement Tool

`measure_latency.py` - Automated input-to-display latency measurement (Wayland/GNOME).

**How it works:**

1. Records screen via GNOME Shell Screencast D-Bus API
2. Sends keystroke at known time via ydotool
3. Frame-diffs consecutive frames to detect when character appears
4. Reports latency in milliseconds

**Setup on atlas (SPICE client machine):**

```bash
# ydotool, gstreamer plugins installed via ansible/atlas.yaml
cd ~/code/ducktape/ansible && ansible-playbook atlas.yaml --tags packages,ydotool,users
# Reboot or re-login for input group membership to take effect
```

**Setup in VM (wyrm) via SPICE:**

```bash
# Switch to VT (Ctrl+Alt+F1), then open nvim with no config, no cursor blink, insert mode
nvim --clean -c "set guicursor=a:blinkon0" -c "startinsert"
```

**Run measurement:**

```bash
cd ~/code/ducktape
./investigations/spice-lag/measure_latency.py
```

**Options:**

- `--samples N` - Number of measurements
- `--fps N` - Recording framerate (higher = more precision)
- `--keep-video` - Keep recordings for debugging

## Next Steps

1. Run `measure_latency.py` to get baseline latency measurement
2. Try Option 1 - change `vga: qxl` to `vga: virtio-gl` and re-measure
3. Compare before/after to quantify improvement
