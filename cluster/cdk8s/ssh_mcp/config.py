"""The shared SSH MCP service, credential, and target configuration contract.

Keep SSH targets, their private-key paths, and pinned host keys in one place so the
generated ssh-mcp ConfigMap and sshpiper Pipe cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address

from cluster.cdk8s.home_lan import HOME_LAN
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from ssh_mcp_server.server import SshSettings
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "ssh-mcp"
NAMESPACE = NAME
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
MCP_URL = f"{SERVICE.url}/mcp"
BEARER_SECRET_NAME = "ssh-mcp-bearer"
BEARER_SECRET_KEY = "bearer-token"
CONFIG_MAP_NAME = "config"
CONFIG_DIR = "/etc/ssh-mcp"

DEVBOX_HOST_KEY = "ssh_keys/public-coder-devbox-host.pub"
AGENT_DOWNSTREAM_KEY = "ssh_keys/public-coder-agent-devbox.pub"
SWITCH_HOST_KEY = "ssh_keys/home-switch-host-key.pub"
SWITCH_AGENT_PUBLIC_KEY = "ssh_keys/home-switch-agentplane.pub"
NEBULA_SUFFIX = ".nebula.allegedly.works"
NEBULA_HOST_KEY = "ssh_keys/{name}-host-ed25519.pub"


@dataclass(frozen=True)
class NebulaDestination:
    """An SSH destination addressed through Nebula."""

    name: str

    @property
    def host(self) -> str:
        return f"{self.name}{NEBULA_SUFFIX}"

    @property
    def host_key_file(self) -> str:
        return NEBULA_HOST_KEY.format(name=self.name)


@dataclass(frozen=True)
class ServiceDestination:
    """An SSH destination selected by a Kubernetes Service's Pod endpoints."""

    service: ServiceRef
    host_key_file: str

    @property
    def host(self) -> str:
        return self.service.fqdn


@dataclass(frozen=True)
class LanDestination:
    """An SSH destination addressed directly on the home LAN."""

    address: IPv4Address
    host_key_file: str

    @property
    def host(self) -> str:
        return str(self.address)


Destination = NebulaDestination | ServiceDestination | LanDestination


@dataclass(frozen=True)
class Target:
    destination: Destination
    user: str
    identity_file: str

    def ssh_settings(self) -> dict[str, str]:
        return {"host": self.destination.host, "user": self.user, "identity_file": self.identity_file}


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
    switch_agent_public_key: str

    @property
    def destinations(self) -> tuple[Destination, ...]:
        """Unique SSH destinations, grouped by their declared network path."""
        return _ordered_destinations(self.targets)


def _targets(devbox_ssh: ServiceRef) -> tuple[Target, ...]:
    """A missing identity file leaves a target listed as unavailable, never silently omitted."""
    return (
        *_targets_for(NebulaDestination("wyrm2"), ("agentydragon", "root"), "keys", "wyrm2"),
        *_targets_for(NebulaDestination("rugged"), ("agentydragon", "root"), "keys", "rugged"),
        *_targets_for(
            ServiceDestination(devbox_ssh, DEVBOX_HOST_KEY),
            ("root", "coder"),
            "keys-public-coder-devbox",
            "public-coder-devbox",
        ),
        *_targets_for(NebulaDestination("atlas"), ("root", "agentydragon"), "keys-atlas", "atlas"),
        Target(
            LanDestination(HOME_LAN.switch, SWITCH_HOST_KEY),
            "agentplane",
            f"{CONFIG_DIR}/keys-agentplane/home-switch-agentplane",
        ),
    )


def _targets_for(
    destination: Destination, users: tuple[str, ...], identity_directory: str, identity_prefix: str
) -> tuple[Target, ...]:
    """Build account targets whose identity paths share a directory and filename prefix."""
    return tuple(
        Target(destination, user, f"{CONFIG_DIR}/{identity_directory}/{identity_prefix}-{user}") for user in users
    )


def _ordered_destinations(targets: tuple[Target, ...]) -> tuple[Destination, ...]:
    unique = tuple(dict.fromkeys(target.destination for target in targets))
    return (
        *(destination for destination in unique if isinstance(destination, NebulaDestination)),
        *(destination for destination in unique if isinstance(destination, ServiceDestination)),
        *(destination for destination in unique if isinstance(destination, LanDestination)),
    )


def _read_input(relative: str) -> str:
    return get_required_path(own_repo_rlocation(relative)).read_text()


def _host_key(relative: str) -> str:
    key_type, key, *_comment = _read_input(relative).split()
    return f"{key_type} {key}"


def load(devbox_ssh: ServiceRef) -> SshMcpConfig:
    """Read canonical key inputs and derive the SSH endpoint from its Service reference."""
    devbox_host = devbox_ssh.fqdn
    key_type, key, *_comment = _read_input(DEVBOX_HOST_KEY).split()
    targets = _targets(devbox_ssh)
    settings: dict[str, object] = {
        "known_hosts_file": f"{CONFIG_DIR}/known_hosts",
        "command_timeout_seconds": 300,
        "connect_timeout_seconds": 10,
        "output_limit_bytes": 65536,
        "max_concurrent_executions": 4,
        "targets": [target.ssh_settings() for target in targets],
    }
    SshSettings.model_validate(settings)
    destinations = _ordered_destinations(targets)
    known_hosts = "".join(
        f"{destination.host} {_host_key(destination.host_key_file)}\n" for destination in destinations
    )
    agent_downstream_key = _read_input(AGENT_DOWNSTREAM_KEY)
    switch_agent_public_key = " ".join(_read_input(SWITCH_AGENT_PUBLIC_KEY).split()[:2])
    return SshMcpConfig(
        devbox_host=devbox_host,
        devbox_key_type=key_type,
        devbox_key=key,
        devbox_port=devbox_ssh.port.number,
        targets=targets,
        settings=settings,
        known_hosts=known_hosts,
        agent_downstream_key=agent_downstream_key,
        switch_agent_public_key=switch_agent_public_key,
    )
