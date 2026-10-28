"""cert-manager's namespaced `Issuer`, using the same typed class construction as the
`ClusterIssuer` wrapper and exposing the CA issuer mode used for Agentplane runner credentials.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from cert_manager_issuer_crds.io.cert_manager import Issuer as _Issuer, IssuerSpec, IssuerSpecCa
from constructs import Construct


class Issuer(_Issuer):
    """A namespaced CA Issuer backed by a Certificate-managed Secret."""

    def __init__(self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, ca: IssuerSpecCa) -> None:
        super().__init__(scope, id, metadata=metadata, spec=IssuerSpec(ca=ca))
