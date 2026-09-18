"""The Alembic-style migrate initContainer shared by every Agentplane service (app,
egress, actions): same resources and securityContext everywhere -- only the image and
migration env vary.
"""

from __future__ import annotations

from cdk8s import Size
from cdk8s_plus_34 import (
    Capability,
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
)


def migrate_init_container(image: str, *, env_variables: dict[str, EnvValue]) -> ContainerProps:
    return ContainerProps(
        name="migrate",
        image=image,
        image_pull_policy=ImagePullPolicy.ALWAYS,
        env_variables=env_variables,
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25)),
            memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
        ),
        security_context=ContainerSecurityContextProps(
            allow_privilege_escalation=False,
            capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
            read_only_root_filesystem=False,
        ),
    )
