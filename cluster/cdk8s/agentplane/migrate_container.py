"""The shared migration init container for Agentplane services: resources and security
context stay consistent while each service supplies its own image and migration environment.
"""

from __future__ import annotations

from cdk8s import Size
from cdk8s_plus_34 import (
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
)

_DEFAULT_MEMORY_LIMIT = Size.mebibytes(256)


def migrate_init_container(
    image: str,
    *,
    name: str,
    env_variables: dict[str, EnvValue],
    command: list[str] | None = None,
    image_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
    memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
) -> ContainerProps:
    return ContainerProps(
        name=name,
        image=image,
        command=command,
        image_pull_policy=image_pull_policy,
        env_variables=env_variables,
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25)),
            memory=MemoryResources(request=Size.mebibytes(64), limit=memory_limit),
        ),
        # Writable: its root filesystem writes are unaudited.
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
