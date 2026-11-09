"""Ergonomic wrapper for Agentplane's own `EgressCredential` CRD
(agentplane/crds/manifests/crd-egresscredentials.yaml), following cdk8s-plus's own
construction pattern: a class named after the kind, and a named `@staticmethod` factory group for
`source`'s real variant shapes.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredential as _EgressCredential,
    EgressCredentialSpec,
    EgressCredentialSpecSource,
    EgressCredentialSpecSourceKubernetesServiceAccount,
    EgressCredentialSpecSourceProjectedWorkloadToken,
    EgressCredentialSpecSourceSecretRef,
    EgressCredentialSpecTargets,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct


class Source:
    """`EgressCredentialSpecSource`'s variant shapes: the CRD schema declares them `oneOf`, so
    exactly one factory's result is ever set. `secret_ref` resolves a Secret key in the proxy's
    configured credentials namespace; `projected_workload_token` selects that same Pod's identity
    minted for another audience; `kubernetes_service_account` has the proxy impersonate an account.
    """

    @staticmethod
    def secret_ref(*, name: str, key: str) -> EgressCredentialSpecSource:
        return EgressCredentialSpecSource(secret_ref=EgressCredentialSpecSourceSecretRef(name=name, key=key))

    @staticmethod
    def projected_workload_token(*, audience: str) -> EgressCredentialSpecSource:
        return EgressCredentialSpecSource(
            projected_workload_token=EgressCredentialSpecSourceProjectedWorkloadToken(audience=audience)
        )

    @staticmethod
    def kubernetes_service_account(*, service_account: ServiceAccount) -> EgressCredentialSpecSource:
        namespace = service_account.metadata.namespace
        assert namespace is not None, f"{service_account.name=} has no namespace"
        return EgressCredentialSpecSource(
            kubernetes_service_account=EgressCredentialSpecSourceKubernetesServiceAccount(
                namespace=namespace, name=service_account.name
            )
        )


class EgressCredential(_EgressCredential):
    """Agentplane's `EgressCredential`: keywords are `EgressCredentialSpec` fields under their own
    names. Build `source` with `Source`'s factories.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        description: str,
        source: EgressCredentialSpecSource,
        targets: Sequence[EgressCredentialSpecTargets],
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=EgressCredentialSpec(description=description, source=source, targets=list(targets)),
        )
