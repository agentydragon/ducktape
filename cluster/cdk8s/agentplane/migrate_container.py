"""The Alembic-style migrate initContainer shared by every Agentplane service (app,
egress, actions): same resources and securityContext everywhere -- only the image and
migration env vary.
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


def migrate_init_container(image: str, *, name: str, env_variables: dict[str, EnvValue]) -> ContainerProps:
    return ContainerProps(
        name=name,
        image=image,
        image_pull_policy=ImagePullPolicy.ALWAYS,
        env_variables=env_variables,
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25)),
            memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
        ),
        # Writable: its root filesystem writes are unaudited.
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
