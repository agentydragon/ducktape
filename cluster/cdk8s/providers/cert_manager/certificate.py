"""Ergonomic wrapper for cert-manager's `Certificate`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as `Certificate(scope, id, *,
metadata, ...)`. `CertificateSpec` has no real variant shapes at this level -- `privateKey`,
`secretTemplate` and `issuerRef` are each a single fixed shape, not alternatives -- so every
other keyword below is a `CertificateSpec` field under its own name and type; `None` leaves it
unset, so cert-manager's own default applies.

`CertificatePrivateKey` groups `CertificateSpecPrivateKey`'s real algorithm/size variance
(RSA/ECDSA/Ed25519, each with its own valid sizes) under named `@staticmethod` factories that
return the struct itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cert_manager_crds.io.cert_manager import (
    Certificate as _Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecSecretTemplate,
    CertificateSpecUsages,
)
from constructs import Construct


class Certificate(_Certificate):
    """`issuer_ref` and `secret_name` are the only fields the CRD itself requires; every
    other keyword is optional and CRD-defaulted when omitted.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        secret_name: str,
        issuer_ref: CertificateSpecIssuerRef,
        is_ca: bool | None = None,
        common_name: str | None = None,
        dns_names: Sequence[str] | None = None,
        ip_addresses: Sequence[str] | None = None,
        duration: str | None = None,
        renew_before: str | None = None,
        private_key: CertificateSpecPrivateKey | None = None,
        secret_template: CertificateSpecSecretTemplate | None = None,
        usages: Sequence[CertificateSpecUsages] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=CertificateSpec(
                secret_name=secret_name,
                issuer_ref=issuer_ref,
                is_ca=is_ca,
                common_name=common_name,
                dns_names=list(dns_names) if dns_names else None,
                ip_addresses=list(ip_addresses) if ip_addresses else None,
                duration=duration,
                renew_before=renew_before,
                private_key=private_key,
                secret_template=secret_template,
                usages=list(usages) if usages else None,
            ),
        )


class CertificatePrivateKey:
    """Named factories for `CertificateSpecPrivateKey`'s real algorithm/size combinations."""

    @staticmethod
    def ecdsa_p256(rotation_policy: CertificateSpecPrivateKeyRotationPolicy | None = None) -> CertificateSpecPrivateKey:
        return CertificateSpecPrivateKey(
            algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256, rotation_policy=rotation_policy
        )
