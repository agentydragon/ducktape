from __future__ import annotations

import os

# Docker network for agent containers, configurable via PROPS_DOCKER_NETWORK.
# Defaults to "props-agents" (production Docker Compose network).
# Set to "host" in CI/e2e tests (Firecracker, Podman) where no bridge network exists.
PROPS_NETWORK_NAME = os.environ.get("PROPS_DOCKER_NETWORK", "props-agents")
