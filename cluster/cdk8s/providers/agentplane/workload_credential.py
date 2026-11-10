"""Ergonomic wrapper for Agentplane's own `WorkloadCredential` CRD
(agentplane/crds/manifests/crd-workloadcredentials.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind.

cdk8s renders only standing records: no owner and no binding, so the issuer keeps one credential ID
and names its Secret `svid-<name>` (agentplane/docs/workload_authentication.md). Owned, bound
records are their controllers' to write.
"""

from __future__ import annotations

from agentplane_workloadcredential_crds.works.allegedly.agentplane import (
    WorkloadCredential as _WorkloadCredential,
    WorkloadCredentialSpec,
    WorkloadCredentialSpecSubject,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import IServiceAccount
from constructs import Construct


class WorkloadCredential(_WorkloadCredential):
    """A standing workload's `WorkloadCredential`, whose subject is a ServiceAccount in its own namespace."""

    def __init__(self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, subject: IServiceAccount) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=WorkloadCredentialSpec(subject=WorkloadCredentialSpecSubject(service_account=subject.name)),
        )
