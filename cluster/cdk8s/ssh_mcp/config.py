"""The shared SSH MCP service, credential, and target configuration contract.

The ssh-mcp ConfigMap and sshpiper's Pipe both pin the public-coder devbox from its
Service and committed public host key. Keep those inputs and the SSH target roster
here so the two generated outputs cannot disagree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from x.ssh_mcp_server.server import SshSettings

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
NEBULA_SUFFIX = ".nebula.allegedly.works"
NEBULA_HOST_KEY = "ssh_keys/{name}-host-ed25519.pub"

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
        return _nebula_hostnames(self.targets)


def _nebula_hostnames(targets: tuple[Target, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            target.host.removesuffix(NEBULA_SUFFIX) for target in targets if target.host.endswith(NEBULA_SUFFIX)
        )
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
    host: str, users: tuple[str, ...], identity_directory: str, identity_prefix: str
) -> tuple[Target, ...]:
    """Build targets whose identity paths share a directory and filename prefix."""
    return tuple(Target(host, user, f"{CONFIG_DIR}/{identity_directory}/{identity_prefix}-{user}") for user in users)


def _read_input(relative: str) -> str:
    return get_required_path(own_repo_rlocation(relative)).read_text()


def _host_key(relative: str) -> str:
    key_type, key, *_comment = _read_input(relative).split()
    return f"{key_type} {key}"


def load(devbox_ssh: ServiceRef) -> SshMcpConfig:
    """Read canonical key inputs and derive the SSH endpoint from its Service reference."""
    devbox_host = devbox_ssh.fqdn
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
    known_hosts = "".join(
        f"{name}{NEBULA_SUFFIX} {_host_key(NEBULA_HOST_KEY.format(name=name))}\n" for name in _nebula_hostnames(targets)
    )
    known_hosts += f"{devbox_host} {host_key}\n"
    agent_downstream_key = _read_input(AGENT_DOWNSTREAM_KEY)
    return SshMcpConfig(
        devbox_host=devbox_host,
        devbox_key_type=key_type,
        devbox_key=key,
        devbox_port=devbox_ssh.port.number,
        targets=targets,
        settings=settings,
        known_hosts=known_hosts,
        agent_downstream_key=agent_downstream_key,
    )
