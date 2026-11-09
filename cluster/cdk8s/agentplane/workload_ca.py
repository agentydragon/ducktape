"""The Agentplane workload CA: the `agentplane-workload` ClusterIssuer every Agentplane SVID comes
from, the trust-manager Bundle carrying that CA alone, and the admission policy naming who may
request a certificate from it.

The CA is an intermediate under the cluster root, with its key in cert-manager's namespace. Its
leaves still chain to `cluster-internal-ca-bundle`, so clients verifying a service's server
certificate keep that bundle. Verifiers of client SVIDs trust only this Bundle, so a
`cluster-internal-ca` leaf naming an Agentplane URI does not authenticate.

The objects are cluster-scoped and shared by every environment and standing workload namespace.
"""

from __future__ import annotations

from collections.abc import Set

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from cert_manager_clusterissuer_crds.io.cert_manager import ClusterIssuerSpecCa
from cert_manager_crds.io.cert_manager import (
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import (
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from agentplane.sandbox_controller.settings import CertificateIssuer, IssuerKind
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.cert_manager.cluster_ca import INTERNAL_ISSUER, LONG_LIVED_CA
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.providers.cert_manager.cluster_issuer import ClusterIssuer

ISSUER = CertificateIssuer(name="agentplane-workload", kind=IssuerKind.CLUSTER_ISSUER)
ISSUER_REF = CertificateSpecIssuerRef(name=ISSUER.name, kind=ISSUER.kind)
BUNDLE = "agentplane-workload-ca-bundle"
BUNDLE_KEY = "ca-certificates.crt"
# A ClusterIssuer reads its CA from cert-manager's cluster resource namespace.
_CA_NAMESPACE = "cert-manager"
_CA_SECRET = "agentplane-workload-ca"
POLICY = "agentplane-workload-certificates"
_GROUP = "cert-manager.io"


def _username(account: ServiceAccountRef) -> str:
    return f"system:serviceaccount:{account.namespace}:{account.name}"


def add_workload_ca(
    scope: Construct,
    *,
    trust_namespaces: Set[str],
    sandbox_controllers: Set[ServiceAccountRef],
    service_requester: str,
    cert_manager: ServiceAccountRef,
) -> None:
    """The CA, its ClusterIssuer, its Bundle in `trust_namespaces`, and the admission policy.

    Each of `sandbox_controllers` may request Runtime SVIDs in its own namespace;
    `service_requester`, the username that applies rendered manifests, may request one-segment
    service URIs; only `cert_manager` may create the CertificateRequests.
    """
    Certificate(
        scope,
        "workload-ca-certificate",
        metadata=ApiObjectMetadata(name=_CA_SECRET, namespace=_CA_NAMESPACE),
        secret_name=_CA_SECRET,
        is_ca=True,
        common_name=_CA_SECRET,
        **LONG_LIVED_CA,
        # A renewal keeps the key, so leaves issued before it still verify against the new Bundle.
        private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.NEVER),
        usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
        issuer_ref=INTERNAL_ISSUER,
    )
    ClusterIssuer(
        scope,
        "workload-issuer",
        metadata=ApiObjectMetadata(name=ISSUER.name),
        ca=ClusterIssuerSpecCa(secret_name=_CA_SECRET),
    )
    Bundle(
        scope,
        "workload-trust",
        metadata=ApiObjectMetadata(name=BUNDLE),
        # `tls.crt` of a CA the cluster root signs holds that CA alone: cert-manager leaves the
        # self-signed root out of the chain, and `ca.crt` is the root.
        sources=[BundleSpecSources(secret=BundleSpecSourcesSecret(name=_CA_SECRET, key="tls.crt"))],
        target=BundleSpecTarget(
            config_map=BundleSpecTargetConfigMap(
                key=BUNDLE_KEY,
                metadata=BundleSpecTargetConfigMapMetadata(
                    annotations={"description": "The Agentplane workload CA, which client SVID verifiers trust alone"}
                ),
            ),
            namespace_selector=BundleSpecTargetNamespaceSelector(
                match_expressions=[
                    BundleSpecTargetNamespaceSelectorMatchExpressions(
                        key="kubernetes.io/metadata.name", operator="In", values=sorted(trust_namespaces)
                    )
                ]
            ),
        ),
    )
    _add_admission(
        scope, sandbox_controllers=sandbox_controllers, service_requester=service_requester, cert_manager=cert_manager
    )


def _add_admission(
    scope: Construct,
    *,
    sandbox_controllers: Set[ServiceAccountRef],
    service_requester: str,
    cert_manager: ServiceAccountRef,
) -> None:
    # The objects reach CEL as plain maps: a missing key is an error, so optional fields are read
    # with `?`. cert-manager defaults an empty issuerRef kind to Issuer and group to its own.
    issuer = (
        f"object.spec.?issuerRef.?name.orValue('') == '{ISSUER.name}'"
        f" && object.spec.?issuerRef.?kind.orValue('Issuer') == '{ISSUER.kind}'"
        f" && object.spec.?issuerRef.?group.orValue('') in ['', '{_GROUP}']"
    )
    # The Policy Controller's entry replaces the Sandbox Controller's once it owns Runtime SVID issuance.
    runtime_requesters = [
        f"(request.userInfo.username == '{_username(account)}' && request.namespace == '{account.namespace}'"
        " && variables.uris.all(u, u.matches("
        f"'^spiffe://agentplane/environment-runtime/{account.namespace}/[^/]+/[^/]+$')))"
        for account in sorted(sandbox_controllers, key=lambda account: (account.namespace, account.name))
    ]
    service = (
        f"(request.userInfo.username == '{service_requester}'"
        " && variables.uris.all(u, u.matches('^spiffe://agentplane/[a-z0-9-]+$')))"
    )
    k8s.KubeValidatingAdmissionPolicy(
        scope,
        "workload-certificate-admission",
        metadata=k8s.ObjectMeta(name=POLICY),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Fail",
            match_constraints=k8s.MatchResources(
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=[_GROUP],
                        api_versions=["*"],
                        operations=["CREATE", "UPDATE"],
                        resources=["certificates", "certificaterequests"],
                    )
                ]
            ),
            match_conditions=[
                k8s.MatchCondition(name="workload-issuer", expression=issuer),
                # cert-manager and the garbage collector update metadata and status only.
                k8s.MatchCondition(
                    name="spec-written", expression="request.operation == 'CREATE' || object.spec != oldObject.spec"
                ),
            ],
            variables=[
                k8s.Variable(name="certificate", expression="request.resource.resource == 'certificates'"),
                k8s.Variable(name="uris", expression="object.spec.?uris.orValue([])"),
            ],
            validations=[
                k8s.Validation(
                    expression=f"variables.certificate || request.userInfo.username == '{_username(cert_manager)}'",
                    message="Only cert-manager creates CertificateRequests for the Agentplane workload issuer.",
                ),
                k8s.Validation(
                    expression=(
                        "!object.spec.?isCA.orValue(false)"
                        " && !object.spec.?usages.orValue([]).exists(u, u in ['cert sign', 'crl sign'])"
                    ),
                    message="The Agentplane workload issuer signs no CA.",
                ),
                k8s.Validation(
                    expression=(
                        "!variables.certificate || (size(variables.uris) > 0"
                        f" && ({' || '.join([*runtime_requesters, service])}))"
                    ),
                    message=(
                        "Only a Sandbox Controller (Runtime URIs in its namespace) and the manifest applier"
                        " (one-segment service URIs) request certificates from the Agentplane workload issuer."
                    ),
                ),
            ],
        ),
    )
    k8s.KubeValidatingAdmissionPolicyBinding(
        scope,
        "workload-certificate-admission-binding",
        metadata=k8s.ObjectMeta(name=POLICY),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(policy_name=POLICY, validation_actions=["Deny"]),
    )
