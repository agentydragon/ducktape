---
name: hetzner_vnc_screenshot
description: Take and view screenshots of Hetzner Cloud servers via WebSocket VNC console. Use to diagnose issues when text commands fail — boot problems, unresponsive servers, kernel panics, stuck boot screens, graphical output inspection.
---

# Hetzner VNC Screenshot

Capture screenshots of Hetzner Cloud servers via WebSocket VNC console.

## Usage

```bash
# By server name (requires HCLOUD_TOKEN env var)
hetzner-vnc-screenshot my-server-name --output /tmp/screenshot.png

# With explicit credentials
hetzner-vnc-screenshot --url '<wss_url>' --password '<password>' --output /tmp/screenshot.png

# Custom API token
hetzner-vnc-screenshot my-server --token <api-token> --output /tmp/screenshot.png
```

Then use the Read tool to view the screenshot.

## Use Cases

Debugging boot issues, viewing console when network is unreachable, checking kernel messages, diagnosing maintenance mode, viewing GRUB/BIOS screens.
