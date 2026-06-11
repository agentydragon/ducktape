#!/usr/bin/env python3
"""Wrapper around dockerd that patches /etc/docker/daemon.json before starting.

BuildBuddy's init-dockerd (goinit) overwrites daemon.json with its own
config (registry-mirrors, insecure-registries) via MMDS metadata. There's
no exec property to pass custom dockerd flags or preserve image settings.

This wrapper runs AFTER init-dockerd writes daemon.json, so we can merge
in the settings we need. Specifically:

  allow-direct-routing: true
    Docker 28+ adds iptables "raw" table DROP rules for direct access
    filtering when publishing ports. Firecracker's kernel lacks
    CONFIG_IP_NF_RAW, so any "docker run -p" fails without this setting.

The wrapper preserves whatever init-dockerd wrote (registry-mirrors, etc.)
and merges in our required settings before exec'ing the real dockerd binary.
"""

import json
import os
import sys
from pathlib import Path

DAEMON_JSON = Path("/etc/docker/daemon.json")
REAL_DOCKERD = "/usr/bin/dockerd.real"

MERGE_CONFIG = {"allow-direct-routing": True}


def main() -> None:
    if DAEMON_JSON.exists():
        config = json.loads(DAEMON_JSON.read_text())
        config.update(MERGE_CONFIG)
    else:
        DAEMON_JSON.parent.mkdir(parents=True, exist_ok=True)
        config = dict(MERGE_CONFIG)

    DAEMON_JSON.write_text(json.dumps(config, indent=2) + "\n")

    os.execv(REAL_DOCKERD, [REAL_DOCKERD, *sys.argv[1:]])


if __name__ == "__main__":
    main()
