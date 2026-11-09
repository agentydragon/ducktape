"""Who may request a certificate from the Agentplane workload CA (`cluster_ca.WORKLOAD_ISSUER`).

Without this policy the ClusterIssuer would sign any Agentplane URI for anyone allowed to create a
Certificate in any namespace. The policy is cluster-scoped: one per cluster, naming every requester.
"""

from __future__ import annotations

from collections.abc import Set

from cdk8s_plus_34 import k8s
from constructs import Construct

from agentplane.sandbox_controller.settings import CertificateIssuer, IssuerKind
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.cert_manager.cluster_ca import WORKLOAD_ISSUER

# The issuer the Sandbox Controller requests Runtime certificates from.
RUNNER_CERTIFICATE_ISSUER = CertificateIssuer(name=WORKLOAD_ISSUER.name, kind=IssuerKind.CLUSTER_ISSUER)
POLICY = "agentplane-workload-certificates"
_GROUP = "cert-manager.io"


def _username(account: ServiceAccountRef) -> str:
    return f"system:serviceaccount:{account.namespace}:{account.name}"


def add_workload_certificate_admission(
    scope: Construct,
    *,
    sandbox_controllers: Set[ServiceAccountRef],
    workload_credential_issuers: Set[ServiceAccountRef],
    service_requester: str,
    cert_manager: ServiceAccountRef,
) -> None:
    """Each of `sandbox_controllers` may request Runtime SVIDs in its own namespace; each of
    `workload_credential_issuers` may request a WorkloadCredential's URI in any Certificate's own namespace;
    `service_requester`, the username that applies rendered manifests, may request one-segment
    service URIs; only `cert_manager` may create the CertificateRequests. Nobody gets a CA."""
    # The objects reach CEL as plain maps: a missing key is an error, so optional fields are read
    # with `?`. cert-manager defaults an empty issuerRef kind to Issuer and group to its own.
    issuer = (
        f"object.spec.?issuerRef.?name.orValue('') == '{WORKLOAD_ISSUER.name}'"
        f" && object.spec.?issuerRef.?kind.orValue('Issuer') == '{WORKLOAD_ISSUER.kind}'"
        f" && object.spec.?issuerRef.?group.orValue('') in ['', '{_GROUP}']"
    )
    # The Policy Controller's entry replaces the Sandbox Controller's once it owns Runtime SVID issuance.
    runtime_requesters = [
        f"(request.userInfo.username == '{_username(account)}' && request.namespace == '{account.namespace}'"
        " && variables.uris.all(u, u.matches("
        f"'^spiffe://agentplane/environment-runtime/{account.namespace}/[^/]+/[^/]+$')))"
        for account in sorted(sandbox_controllers, key=lambda account: (account.namespace, account.name))
    ]
    # A record's URI names the record's namespace, which is the Certificate's: RBAC limits where an
    # issuer may create Certificates.
    record_requesters = [
        f"(request.userInfo.username == '{_username(account)}'"
        " && variables.uris.all(u, u.matches("
        "'^spiffe://agentplane/' + request.namespace + '/[a-z0-9]([a-z0-9.-]*[a-z0-9])?/[0-9a-f]{32}$')))"
        for account in sorted(workload_credential_issuers, key=lambda account: (account.namespace, account.name))
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
                        f" && ({' || '.join([*runtime_requesters, *record_requesters, service])}))"
                    ),
                    message=(
                        "Only a Sandbox Controller (Runtime URIs in its namespace), the WorkloadCredential issuer"
                        " (record URIs in the Certificate's namespace) and the manifest applier (one-segment service"
                        " URIs) request certificates from the Agentplane workload issuer."
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
