# Parked public-coder-agent image CI

Workflows and smoke script that build the OpenClaw image and the public-coder devbox
containerDisk, parked with the instance (<../../../k8s/agents/public-coder-agent/README.md>).

To resume, move `openclaw-image.yml` and `public-coder-devbox-image.yml` back to
`.github/workflows/` and `smoke-openclaw-image.sh` to `.github/scripts/`, then let them publish
fresh images before unparking the instance.
