"""Named factories for cert-manager's CertificateSpecPrivateKey variants."""

from __future__ import annotations

from cert_manager_crds.io.cert_manager import (
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecPrivateKeyRotationPolicy,
)


class CertificatePrivateKey:
    """Named factories for `CertificateSpecPrivateKey`'s real algorithm/size combinations."""

    @staticmethod
    def ecdsa_p256(rotation_policy: CertificateSpecPrivateKeyRotationPolicy | None = None) -> CertificateSpecPrivateKey:
        return CertificateSpecPrivateKey(
            algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256, rotation_policy=rotation_policy
        )

    @staticmethod
    def rsa_2048(rotation_policy: CertificateSpecPrivateKeyRotationPolicy | None = None) -> CertificateSpecPrivateKey:
        return CertificateSpecPrivateKey(
            algorithm=CertificateSpecPrivateKeyAlgorithm.RSA, size=2048, rotation_policy=rotation_policy
        )
