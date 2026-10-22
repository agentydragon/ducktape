# Parked public-coder-agent image CI

The OpenClaw image workflow and smoke script are parked with the instance (<../README.md>).

To resume, move `openclaw-image.yml` back to `.github/workflows/` and
`smoke-openclaw-image.sh` to `.github/scripts/`, then let the workflow publish a fresh image
before unparking OpenClaw. The KubeVirt devbox and its image workflow were retired.
