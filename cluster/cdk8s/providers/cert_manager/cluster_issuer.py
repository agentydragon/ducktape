"""cert-manager's `ClusterIssuer`, following cdk8s-plus's own construction pattern: a class
named after the kind, constructed as `ClusterIssuer(scope, id, *, metadata, ...)` with the
`ClusterIssuerSpec` issuer-type fields under their own names and types.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from cert_manager_clusterissuer_crds.io.cert_manager import (
    ClusterIssuer as _ClusterIssuer,
    ClusterIssuerSpec,
    ClusterIssuerSpecAcme,
    ClusterIssuerSpecCa,
    ClusterIssuerSpecSelfSigned,
)
from constructs import Construct


class ClusterIssuer(_ClusterIssuer):
    """cert-manager's webhook admits exactly one issuer type. The keywords cover the ones this
    repo builds; the CRD also has `vault` and `venafi`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        self_signed: ClusterIssuerSpecSelfSigned | None = None,
        ca: ClusterIssuerSpecCa | None = None,
        acme: ClusterIssuerSpecAcme | None = None,
    ) -> None:
        super().__init__(
            scope, id, metadata=metadata, spec=ClusterIssuerSpec(self_signed=self_signed, ca=ca, acme=acme)
        )
