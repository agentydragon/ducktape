"""Ergonomic wrapper for cert-manager's `ClusterIssuer`, following cdk8s-plus's own
construction pattern: a class named after the kind, with named `@classmethod` factories
grouping `ClusterIssuerSpec`'s real discriminated-union shapes (`selfSigned`, `ca`, `acme`,
`vault`, `venafi`) under one type. Factories below cover the three shapes this repo
constructs today -- add another the day a fourth is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cert_manager_clusterissuer_crds.io.cert_manager import (
    ClusterIssuer as _ClusterIssuer,
    ClusterIssuerSpec,
    ClusterIssuerSpecAcme,
    ClusterIssuerSpecAcmePrivateKeySecretRef,
    ClusterIssuerSpecAcmeSolvers,
    ClusterIssuerSpecCa,
    ClusterIssuerSpecSelfSigned,
)
from constructs import Construct


class ClusterIssuer(_ClusterIssuer):
    @classmethod
    def self_signed(cls, scope: Construct, id: str, *, name: str) -> ClusterIssuer:
        return cls(
            scope,
            id,
            metadata=ApiObjectMetadata(name=name),
            spec=ClusterIssuerSpec(self_signed=ClusterIssuerSpecSelfSigned()),
        )

    @classmethod
    def ca(cls, scope: Construct, id: str, *, name: str, secret_name: str) -> ClusterIssuer:
        """Issues from an existing CA keypair Secret (`secret_name`'s `tls.crt`/`tls.key`)."""
        return cls(
            scope,
            id,
            metadata=ApiObjectMetadata(name=name),
            spec=ClusterIssuerSpec(ca=ClusterIssuerSpecCa(secret_name=secret_name)),
        )

    @classmethod
    def acme(
        cls,
        scope: Construct,
        id: str,
        *,
        name: str,
        server: str,
        email: str,
        private_key_secret_name: str,
        solvers: Sequence[ClusterIssuerSpecAcmeSolvers],
    ) -> ClusterIssuer:
        return cls(
            scope,
            id,
            metadata=ApiObjectMetadata(name=name),
            spec=ClusterIssuerSpec(
                acme=ClusterIssuerSpecAcme(
                    server=server,
                    email=email,
                    private_key_secret_ref=ClusterIssuerSpecAcmePrivateKeySecretRef(name=private_key_secret_name),
                    solvers=list(solvers),
                )
            ),
        )
