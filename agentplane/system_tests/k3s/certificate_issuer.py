"""Test-only cert-manager boundary for the bare-k3s system-test stack.

The production chart creates normal cert-manager Certificates and an Issuer. This fixture installs
minimal CRDs and watches those same Certificate objects, then issues real ECDSA certificates from a
root that the fixture publishes through the normal trust ConfigMap. It does not change application
authentication or bypass the mTLS handshake.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import json
import logging
import threading
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from kubernetes import client, config, watch
from kubernetes.client import ApiException

from agentplane.testing.tls import CertificateAuthority

logger = logging.getLogger(__name__)

API_GROUP = "cert-manager.io"
API_VERSION = "v1"
CERTIFICATE_PLURAL = "certificates"
ISSUER_PLURAL = "issuers"
FIXTURE_ANNOTATION = "agentplane.allegedly.works/test-certificate-spec-sha256"
_UTC = datetime.UTC


def fixture_crds() -> list[dict[str, Any]]:
    """Return the minimal structural CRDs needed by the resources rendered in the fixture."""

    def crd(kind: str, plural: str, singular: str) -> dict[str, Any]:
        return {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": f"{plural}.{API_GROUP}"},
            "spec": {
                "group": API_GROUP,
                "scope": "Namespaced",
                "names": {"kind": kind, "plural": plural, "singular": singular},
                "versions": [
                    {
                        "name": API_VERSION,
                        "served": True,
                        "storage": True,
                        "schema": {
                            "openAPIV3Schema": {
                                "type": "object",
                                "properties": {
                                    "spec": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
                                    "status": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
                                },
                            }
                        },
                        "subresources": {"status": {}},
                    }
                ],
            },
        }

    return [crd("Certificate", CERTIFICATE_PLURAL, "certificate"), crd("Issuer", ISSUER_PLURAL, "issuer")]


def issue_certificate(
    spec: dict[str, Any], issuer: CertificateAuthority, *, root_certificate: x509.Certificate | None = None
) -> tuple[bytes, bytes, x509.Certificate]:
    """Issue a certificate from the fields in an actual cert-manager Certificate.spec.

    `root_certificate` adds the intermediate chain to the returned TLS certificate. A CA request
    should be signed directly by the fixture root; leaf requests are signed by the CA Secret created
    from that request.
    """
    private_key_spec = spec.get("privateKey", {})
    algorithm = private_key_spec.get("algorithm", "ECDSA")
    if algorithm != "ECDSA" or private_key_spec.get("size", 256) != 256:
        raise ValueError(f"fixture signer supports only ECDSA P-256, requested {private_key_spec!r}")

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(_UTC)
    not_before = max(now - datetime.timedelta(minutes=5), issuer.cert.not_valid_before_utc)
    requested_not_after = now + _duration(spec.get("duration", "24h"))
    not_after = min(requested_not_after, issuer.cert.not_valid_after_utc)
    if not_after <= now:
        raise ValueError("issuer certificate has expired")

    # cert-manager does not infer commonName from URI SANs, and workload SPIFFE URIs are usually
    # longer than the X.509 commonName limit (64 bytes).
    common_name = spec.get("commonName") or "agentplane-test-certificate"
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(issuer.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )

    alternative_names: list[x509.GeneralName] = [x509.UniformResourceIdentifier(uri) for uri in spec.get("uris", [])]
    alternative_names.extend(x509.DNSName(name) for name in spec.get("dnsNames", []))
    alternative_names.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in spec.get("ipAddresses", []))
    if alternative_names:
        builder = builder.add_extension(x509.SubjectAlternativeName(alternative_names), critical=False)

    usages = set(spec.get("usages", []))
    supported_usages = {"digital signature", "key encipherment", "client auth", "server auth", "cert sign", "crl sign"}
    if unsupported := usages - supported_usages:
        raise ValueError(f"fixture signer does not support requested certificate usages {sorted(unsupported)}")
    is_ca = spec.get("isCA", False)
    if is_ca:
        if "cert sign" not in usages or "crl sign" not in usages:
            raise ValueError("CA Certificate must request both cert sign and crl sign usages")
        key_usage = x509.KeyUsage(
            digital_signature=False,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        )
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    else:
        if usages & {"cert sign", "crl sign"}:
            raise ValueError("leaf Certificate cannot request cert sign or crl sign usages")
        key_usage = x509.KeyUsage(
            digital_signature="digital signature" in usages,
            content_commitment=False,
            key_encipherment="key encipherment" in usages,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=False,
            crl_sign=False,
            encipher_only=False,
            decipher_only=False,
        )
        builder = builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    builder = builder.add_extension(key_usage, critical=True)

    extended_usages = []
    if "client auth" in usages:
        extended_usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    if "server auth" in usages:
        extended_usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if extended_usages:
        builder = builder.add_extension(x509.ExtendedKeyUsage(extended_usages), critical=False)

    certificate = builder.sign(issuer.key, hashes.SHA256())
    certificate_chain = certificate.public_bytes(serialization.Encoding.PEM)
    if root_certificate is not None:
        certificate_chain += root_certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return certificate_chain, key_pem, certificate


def _duration(value: str) -> datetime.timedelta:
    """Parse the fixed cert-manager duration forms used by this chart."""
    if not value or not value.endswith("h"):
        raise ValueError(f"fixture signer expects an hour-based certificate duration, got {value!r}")
    try:
        return datetime.timedelta(hours=float(value[:-1]))
    except ValueError as error:
        raise ValueError(f"invalid certificate duration {value!r}") from error


def _pem_certificate(value: str | bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificate(value.encode() if isinstance(value, str) else value)


def _pem_key(value: str | bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(value.encode() if isinstance(value, str) else value, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("fixture signer Secret does not contain an ECDSA private key")
    return key


def _secret_pem(secret: Any, key: str) -> bytes:
    if secret.data is None or key not in secret.data:
        raise ValueError(f"CA Secret {secret.metadata.namespace}/{secret.metadata.name} has no {key}")
    return base64.b64decode(secret.data[key], validate=True)


class FixtureCertificateIssuer:
    """Watch actual cert-manager Certificate resources and reconcile their TLS Secrets/status."""

    def __init__(self, kubeconfig: str, root: CertificateAuthority) -> None:
        self.kubeconfig = kubeconfig
        self.root = root
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._core: client.CoreV1Api | None = None
        self._custom: client.CustomObjectsApi | None = None

    @staticmethod
    def install_crds(kubectl: Any) -> None:
        kubectl(
            "apply", "-f", "-", input_text=json.dumps({"items": fixture_crds(), "kind": "List", "apiVersion": "v1"})
        )
        kubectl(
            "wait",
            "--for=condition=Established",
            "--timeout=60s",
            "crd/certificates.cert-manager.io",
            "crd/issuers.cert-manager.io",
        )

    def __enter__(self) -> FixtureCertificateIssuer:
        config.load_kube_config(config_file=self.kubeconfig)
        self._core = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()
        self._thread = threading.Thread(target=self._run, name="fixture-certificate-issuer", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            self._stop.set()
            raise TimeoutError("fixture certificate signer did not list Certificates within 10 seconds")
        self.check()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=12)
            if self._thread.is_alive() and exc_value is None:
                raise TimeoutError("fixture certificate signer watch did not stop")
        if exc_value is None:
            self.check()

    def check(self) -> None:
        if self._failure is not None:
            raise RuntimeError("fixture certificate signer failed") from self._failure

    def _run(self) -> None:
        assert self._custom is not None
        try:
            while not self._stop.is_set():
                listing = self._custom.list_cluster_custom_object(API_GROUP, API_VERSION, CERTIFICATE_PLURAL)
                self._reconcile_all(listing["items"])
                self._ready.set()
                resource_version = listing["metadata"]["resourceVersion"]
                watcher = watch.Watch()
                try:
                    for event in watcher.stream(
                        self._custom.list_cluster_custom_object,
                        API_GROUP,
                        API_VERSION,
                        CERTIFICATE_PLURAL,
                        resource_version=resource_version,
                        timeout_seconds=10,
                    ):
                        if self._stop.is_set():
                            break
                        event_object = event["object"]
                        resource_version = event_object["metadata"]["resourceVersion"]
                        self._reconcile_all(
                            self._custom.list_cluster_custom_object(API_GROUP, API_VERSION, CERTIFICATE_PLURAL)["items"]
                        )
                finally:
                    watcher.stop()
        except BaseException as error:
            self._failure = error
            self._ready.set()
            logger.exception("fixture certificate signer stopped")

    def _reconcile_all(self, certificates: list[dict[str, Any]]) -> None:
        assert self._custom is not None
        assert self._core is not None
        self._reconcile_issuers()
        for certificate in certificates:
            try:
                self._reconcile_certificate(certificate)
            except BaseException as error:
                self._set_certificate_failure(certificate, error)
                raise

    def _reconcile_issuers(self) -> None:
        assert self._custom is not None
        assert self._core is not None
        issuers = self._custom.list_cluster_custom_object(API_GROUP, API_VERSION, ISSUER_PLURAL)["items"]
        for issuer in issuers:
            metadata = issuer["metadata"]
            secret_name = issuer.get("spec", {}).get("ca", {}).get("secretName")
            if not secret_name:
                continue
            try:
                self._core.read_namespaced_secret(secret_name, metadata["namespace"])
            except ApiException as error:
                if error.status == 404:
                    continue
                raise
            status = issuer.get("status", {})
            conditions = status.get("conditions", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                continue
            patch = {
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True",
                            "reason": "FixtureCASecretAvailable",
                            "message": "The test fixture CA Secret is available.",
                            "lastTransitionTime": datetime.datetime.now(_UTC).isoformat(),
                        }
                    ]
                }
            }
            self._custom.patch_namespaced_custom_object_status(
                API_GROUP, API_VERSION, metadata["namespace"], ISSUER_PLURAL, metadata["name"], patch
            )

    def _reconcile_certificate(self, certificate: dict[str, Any]) -> None:
        assert self._custom is not None
        assert self._core is not None
        metadata = certificate["metadata"]
        namespace, name = metadata["namespace"], metadata["name"]
        spec = certificate.get("spec", {})
        secret_name = spec.get("secretName")
        if not secret_name:
            raise ValueError(f"Certificate {namespace}/{name} has no spec.secretName")
        spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        status = certificate.get("status", {})
        conditions = status.get("conditions", [])
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        try:
            secret = self._core.read_namespaced_secret(secret_name, namespace)
        except ApiException as error:
            if error.status != 404:
                raise
            secret = None
        if (
            ready
            and secret is not None
            and secret.metadata.annotations is not None
            and secret.metadata.annotations.get(FIXTURE_ANNOTATION) == spec_hash
        ):
            return

        requested_issuer = spec.get("issuerRef", {})
        issuer_kind = requested_issuer.get("kind", "Issuer")
        issuer_name = requested_issuer.get("name")
        if issuer_kind == "ClusterIssuer" and issuer_name == "cluster-internal-ca":
            signing_ca = self.root
            chain_root = None
        elif issuer_kind == "Issuer" and issuer_name:
            custom_issuer = self._custom.get_namespaced_custom_object(
                API_GROUP, API_VERSION, namespace, ISSUER_PLURAL, issuer_name
            )
            ca_secret_name = custom_issuer.get("spec", {}).get("ca", {}).get("secretName")
            if not ca_secret_name:
                return
            try:
                ca_secret = self._core.read_namespaced_secret(ca_secret_name, namespace)
            except ApiException as error:
                if error.status == 404:
                    return
                raise
            signing_cert = _pem_certificate(_secret_pem(ca_secret, "tls.crt"))
            signing_key = _pem_key(_secret_pem(ca_secret, "tls.key"))
            signing_ca = CertificateAuthority(cert=signing_cert, key=signing_key)
            chain_root = signing_cert
        else:
            raise ValueError(f"unsupported fixture issuerRef {requested_issuer!r} on {namespace}/{name}")

        cert_chain, private_key, leaf = issue_certificate(spec, signing_ca, root_certificate=chain_root)
        secret_template = spec.get("secretTemplate", {})
        annotations = {
            **secret_template.get("annotations", {}),
            "cert-manager.io/certificate-name": name,
            FIXTURE_ANNOTATION: spec_hash,
        }
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "annotations": annotations,
                "labels": secret_template.get("labels", {}),
                "ownerReferences": [
                    {
                        "apiVersion": f"{API_GROUP}/{API_VERSION}",
                        "kind": "Certificate",
                        "name": name,
                        "uid": metadata.get("uid", ""),
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            },
            "type": "kubernetes.io/tls",
            "data": {
                "tls.crt": base64.b64encode(cert_chain).decode("ascii"),
                "tls.key": base64.b64encode(private_key).decode("ascii"),
            },
        }
        if secret is None:
            self._core.create_namespaced_secret(namespace, body)
        else:
            self._core.patch_namespaced_secret(secret_name, namespace, body)

        ready_patch = {
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "reason": "FixtureIssued",
                        "message": "Issued from the test fixture CA using Certificate.spec.",
                        "lastTransitionTime": datetime.datetime.now(_UTC).isoformat(),
                    }
                ],
                "notBefore": leaf.not_valid_before_utc.isoformat(),
                "notAfter": leaf.not_valid_after_utc.isoformat(),
                "revision": 1,
            }
        }
        self._custom.patch_namespaced_custom_object_status(
            API_GROUP, API_VERSION, namespace, CERTIFICATE_PLURAL, name, ready_patch
        )
        logger.info("issued test certificate %s/%s from requested Certificate.spec", namespace, name)

    def _set_certificate_failure(self, certificate: dict[str, Any], error: BaseException) -> None:
        assert self._custom is not None
        metadata = certificate["metadata"]
        try:
            self._custom.patch_namespaced_custom_object_status(
                API_GROUP,
                API_VERSION,
                metadata["namespace"],
                CERTIFICATE_PLURAL,
                metadata["name"],
                {
                    "status": {
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "False",
                                "reason": "FixtureSignerError",
                                "message": str(error)[:1024],
                                "lastTransitionTime": datetime.datetime.now(_UTC).isoformat(),
                            }
                        ]
                    }
                },
            )
        except Exception:
            logger.exception(
                "could not report fixture signer error for %s/%s", metadata.get("namespace"), metadata.get("name")
            )
