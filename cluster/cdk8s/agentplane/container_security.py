"""The container-level securityContext override shared by every Agentplane service
whose actual root needs haven't been audited: cdk8s_plus_34 defaults to a hardened
readOnlyRootFilesystem, and silently hardening it here could break the running service.
"""

from __future__ import annotations

from cdk8s_plus_34 import Capability, ContainerSecurityContextProps, ContainerSecutiryContextCapabilities

WRITABLE_ROOT = ContainerSecurityContextProps(
    capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]), read_only_root_filesystem=False
)
