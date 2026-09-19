"""The typed sshpiper Pipe that maps the Agent's devbox route onto its VM."""

from __future__ import annotations

import base64

from cdk8s import ApiObjectMetadata
from constructs import Construct
from sshpiper_pipe_crds.com.sshpiper import Pipe, PipeSpec, PipeSpecFrom, PipeSpecTo, PipeSpecToPrivateKeySecret

from cluster.cdk8s.ssh_mcp_config import SshMcpConfig

NAMESPACE = "public-coder-agent"
PIPE_NAME = "devbox"
PRIVATE_KEY_SECRET = "public-coder-agent-sshpiper-devbox-key"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class PublicCoderSshPipe(Construct):
    """Pins both service DNS spellings to the devbox host key from its canonical source."""

    def __init__(self, scope: Construct, id: str, *, config: SshMcpConfig, downstream_key: str) -> None:
        super().__init__(scope, id)
        # sshpiper may normalize its dialed address differently by version. Trust both
        # the bare Service DNS name and OpenSSH's [host]:port spelling.
        pinned_hosts = (config.devbox_host, f"[{config.devbox_host}]:{config.devbox_port}")
        pinned = "".join(f"{host} {config.devbox_key_type} {config.devbox_key}\n" for host in pinned_hosts)
        Pipe(
            self,
            "pipe",
            metadata=ApiObjectMetadata(name=PIPE_NAME, namespace=NAMESPACE),
            spec=PipeSpec(
                from_=[PipeSpecFrom(username="devbox", authorized_keys_data=_b64(downstream_key))],
                to=PipeSpecTo(
                    host=f"{config.devbox_host}:{config.devbox_port}",
                    # `coder`, never `root`: the devbox authorizes the mapping key for that
                    # account only, so confinement does not depend on the Pipe contents.
                    username="coder",
                    private_key_secret=PipeSpecToPrivateKeySecret(name=PRIVATE_KEY_SECRET),
                    known_hosts_data=_b64(pinned),
                ),
            ),
        )


def construct(scope: Construct, *, config: SshMcpConfig, downstream_key: str) -> PublicCoderSshPipe:
    return PublicCoderSshPipe(scope, "public-coder-devbox-pipe", config=config, downstream_key=downstream_key)
