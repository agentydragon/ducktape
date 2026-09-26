"""Ergonomic wrapper for Agentplane's own `EgressCredential` CRD
(cluster/k8s/agentplane-crds/crd-egresscredentials.yaml), following cdk8s-plus's own
construction pattern: a class named after the kind, and a named `@classmethod` factory group for
`source`'s real variant shapes.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredential as _EgressCredential,
    EgressCredentialSpec,
    EgressCredentialSpecSource,
    EgressCredentialSpecSourceProjectedWorkloadToken,
    EgressCredentialSpecSourceSecretRef,
    EgressCredentialSpecTargets,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct


class Source:
    """`EgressCredentialSpecSource`'s real variant shapes, all three of which this repo uses: the
    CRD schema declares them `oneOf`, so exactly one factory's result is ever set. `secret_ref`
    resolves a Secret key in the proxy's configured credentials namespace; the other two draw on
    the current request's own authenticated identity rather than a stored value --
    `authenticated_workload_token` for the bearer that authenticated the current sidecar request
    or CONNECT tunnel, `projected_workload_token` for that same Pod's identity minted for another
    audience.
    """

    def __init__(self, spec: EgressCredentialSpecSource) -> None:
        self._spec = spec

    def to_spec(self) -> EgressCredentialSpecSource:
        return self._spec

    @classmethod
    def secret_ref(cls, *, name: str, key: str) -> Source:
        return cls(EgressCredentialSpecSource(secret_ref=EgressCredentialSpecSourceSecretRef(name=name, key=key)))

    @classmethod
    def authenticated_workload_token(cls) -> Source:
        # The schema types this branch `maxProperties: 0`: a required, always-empty object. The
        # generated field defaults to unset (`None`), so selecting this branch means passing that
        # empty object explicitly rather than omitting the keyword.
        return cls(EgressCredentialSpecSource(authenticated_workload_token={}))

    @classmethod
    def projected_workload_token(cls, *, audience: str) -> Source:
        return cls(
            EgressCredentialSpecSource(
                projected_workload_token=EgressCredentialSpecSourceProjectedWorkloadToken(audience=audience)
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
