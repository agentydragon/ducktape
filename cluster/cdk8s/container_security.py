"""The container-level securityContext override shared by every service whose actual
root needs haven't been audited: cdk8s_plus_34 defaults to a hardened
readOnlyRootFilesystem, and silently hardening it here could break the running service.

Also holds the drop-ALL capabilities value shared across both builder tiers: the
fluent `ContainerSecurityContextProps.capabilities` field and the raw
`k8s.SecurityContext.capabilities` field. Both are plain jsii value structs (no
construct-tree identity, equality by value), so one shared instance per tier is safe
to reuse across every call site.
"""

from __future__ import annotations

from cdk8s_plus_34 import Capability, ContainerSecurityContextProps, ContainerSecutiryContextCapabilities, k8s

DROP_ALL_CAPABILITIES = ContainerSecutiryContextCapabilities(drop=[Capability.ALL])
K8S_DROP_ALL_CAPABILITIES = k8s.Capabilities(drop=["ALL"])

WRITABLE_ROOT = ContainerSecurityContextProps(
    allow_privilege_escalation=False, capabilities=DROP_ALL_CAPABILITIES, read_only_root_filesystem=False
)
