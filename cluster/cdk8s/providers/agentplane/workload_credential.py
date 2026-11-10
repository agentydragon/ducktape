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
    WorkloadCredentialSpecSubjectIdentity,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.providers.agentplane.identity import Identity


class WorkloadCredential(_WorkloadCredential):
    """A standing workload's `WorkloadCredential`, acting as `subject`, an `Identity` of the policy
    namespace.

    The subject names no UID: git cannot know one, so the record follows the live Identity of that
    name. The CRD forbids re-pointing a subject without a new binding, which a standing record never
    has, so a changed subject replaces the record; the Flux annotation makes kustomize-controller do
    that (delete, then recreate) instead of failing the apply. Replacing a record rotates its
    credential ID, so the workload's `svid-<name>` Secret is reissued and the workload rolls
    (Reloader) before it reaches the proxy again.
    """

    def __init__(self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, subject: Identity) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=WorkloadCredentialSpec(
                subject=WorkloadCredentialSpecSubject(identity=WorkloadCredentialSpecSubjectIdentity(name=subject.name))
            ),
        )
        self.metadata.add_annotation("kustomize.toolkit.fluxcd.io/force", "enabled")
