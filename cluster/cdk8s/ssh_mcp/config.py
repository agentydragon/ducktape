"""The shared SSH MCP service, credential, and target configuration contract.

The ssh-mcp ConfigMap and sshpiper's Pipe both pin the public-coder devbox from its
Service and committed public host key. Keep those inputs and the SSH target roster
here so the two generated outputs cannot disagree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import yaml

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from x.ssh_mcp_server.server import SshSettings

NAME = "ssh-mcp"
NAMESPACE = NAME
SERVICE_NAME = NAME
HTTP_PORT = 8080
MCP_URL = f"http://{SERVICE_NAME}.{NAMESPACE}.svc.cluster.local:{HTTP_PORT}/mcp"
BEARER_SECRET_NAME = "ssh-mcp-bearer"
BEARER_SECRET_KEY = "bearer-token"
CONFIG_MAP_NAME = "ssh-mcp-config"
CONFIG_DIR = "/etc/ssh-mcp"

DEVBOX_HOST_KEY = "ssh_keys/public-coder-devbox-host.pub"
DEVBOX_SERVICE = "cluster/k8s/agents/public-coder-agent/devbox/service.yaml"
AGENT_DOWNSTREAM_KEY = "ssh_keys/public-coder-agent-devbox.pub"
NEBULA_KNOWN_HOSTS = "cluster/k8s/ssh-mcp/known_hosts"

_WYRM2 = "wyrm2.nebula.allegedly.works"
_RUGGED = "rugged.nebula.allegedly.works"
_ATLAS = "atlas.nebula.allegedly.works"


@dataclass(frozen=True)
class Target:
    host: str
    user: str
    identity_file: str


@dataclass(frozen=True)
class SshMcpConfig:
    devbox_host: str
    devbox_key_type: str
    devbox_key: str
    devbox_port: int
    targets: tuple[Target, ...]
    settings: dict[str, object]
    known_hosts: str
    agent_downstream_key: str

    @property
    def nebula_hostnames(self) -> tuple[str, ...]:
        """Unique mesh hostnames in the SSH target roster, preserving target order."""
        suffix = ".nebula.allegedly.works"
        return tuple(
            dict.fromkeys(target.host.removesuffix(suffix) for target in self.targets if target.host.endswith(suffix))
        )


def _targets(devbox_host: str) -> tuple[Target, ...]:
    """A missing identity file leaves a target listed as unavailable, never silently omitted."""
    return (
        *_targets_for(_WYRM2, ("agentydragon", "root"), "keys", "wyrm2"),
        *_targets_for(_RUGGED, ("agentydragon", "root"), "keys", "rugged"),
        *_targets_for(devbox_host, ("root", "coder"), "keys-public-coder-devbox", "public-coder-devbox"),
        *_targets_for(_ATLAS, ("root", "agentydragon"), "keys-atlas", "atlas"),
    )


def _targets_for(
    host: str,
    users: tuple[str, ...],
    identity_directory: str,
    identity_prefix: str,
) -> tuple[Target, ...]:
    """Build targets whose identity paths share a directory and filename prefix."""
    return tuple(
        Target(host, user, f"{CONFIG_DIR}/{identity_directory}/{identity_prefix}-{user}") for user in users
    )


def _read_input(relative: str) -> str:
    return get_required_path(own_repo_rlocation(relative)).read_text()


def load() -> SshMcpConfig:
    """Read canonical host-key inputs and return validated server configuration."""
    service = yaml.safe_load(_read_input(DEVBOX_SERVICE))
    devbox_host = f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local"
    [ssh_port] = service["spec"]["ports"]
    key_type, key, *_comment = _read_input(DEVBOX_HOST_KEY).split()
    targets = _targets(devbox_host)
    settings: dict[str, object] = {
        "known_hosts_file": f"{CONFIG_DIR}/known_hosts",
        "command_timeout_seconds": 300,
        "connect_timeout_seconds": 10,
        "output_limit_bytes": 65536,
        "max_concurrent_executions": 4,
        "targets": [asdict(target) for target in targets],
    }
    SshSettings.model_validate(settings)
    host_key = f"{key_type} {key}"
    known_hosts = _read_input(NEBULA_KNOWN_HOSTS).rstrip()
    if known_hosts:
        known_hosts += "\n"
    known_hosts += f"{devbox_host} {host_key}\n"
    agent_downstream_key = _read_input(AGENT_DOWNSTREAM_KEY)
    return SshMcpConfig(
        devbox_host=devbox_host,
        devbox_key_type=key_type,
        devbox_key=key,
        devbox_port=ssh_port["port"],
        targets=targets,
        settings=settings,
        known_hosts=known_hosts,
        agent_downstream_key=agent_downstream_key,
    )
