"""What the Policy Controller may write as a ManagedServiceAccount's use: its EgressCredential and
one-rule PolicySet, in exactly the shape `kubernetes_accounts.KubernetesAccount` renders for a static
account, acting as the owning account and nothing else.

RBAC cannot name objects the controller creates, so it may create and patch any EgressCredential and
PolicySet in its policy namespace; this policy narrows those writes. An object it did not create as a
managed account's, such as a static account's credential of the same name, stays out of its reach.
"""

from __future__ import annotations

from cdk8s_plus_34 import k8s
from constructs import Construct

from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.agentplane.kubernetes_account_ref import CREDENTIAL_PREFIX

_GROUP = "agentplane.allegedly.works"


def add_managed_account_admission(
    scope: Construct, *, namespace: str, controller: ServiceAccountRef, accounts_namespace: str
) -> None:
    """Admit `controller`'s writes of EgressCredentials and PolicySets only as the use of the
    ManagedServiceAccount controlling them, never taking over one another account or nobody controls."""
    name = f"{namespace}-managed-account-use"
    # The objects reach CEL as plain maps: a missing key is an error, so optional fields are read
    # with `?`.
    k8s.KubeValidatingAdmissionPolicy(
        scope,
        "managed-account-use-admission",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Fail",
            match_constraints=k8s.MatchResources(
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=[_GROUP],
                        api_versions=["*"],
                        operations=["CREATE", "UPDATE"],
                        resources=["egresscredentials", "policysets"],
                    )
                ]
            ),
            match_conditions=[
                k8s.MatchCondition(
                    name="policy-controller",
                    expression=(
                        f"request.userInfo.username == 'system:serviceaccount:{controller.namespace}:{controller.name}'"
                    ),
                )
            ],
            variables=[
                k8s.Variable(name="owners", expression="object.metadata.?ownerReferences.orValue([])"),
                k8s.Variable(
                    name="owned",
                    expression=(
                        "size(variables.owners) == 1"
                        f" && variables.owners[0].apiVersion == '{_GROUP}/v1alpha1'"
                        " && variables.owners[0].kind == 'ManagedServiceAccount'"
                        " && variables.owners[0].?controller.orValue(false)"
                    ),
                ),
                # Guarded: read only once `owned` holds.
                k8s.Variable(name="owner", expression="variables.owners[0]"),
                k8s.Variable(name="credential", expression="request.resource.resource == 'egresscredentials'"),
            ],
            validations=[
                k8s.Validation(
                    expression=f"variables.owned && object.metadata.name == '{CREDENTIAL_PREFIX}' + variables.owner.name",
                    message=(
                        "The Policy Controller writes only a ManagedServiceAccount's use, named for and"
                        " controlled by that account alone."
                    ),
                ),
                k8s.Validation(
                    expression=(
                        "!variables.owned || request.operation != 'UPDATE'"
                        " || oldObject.metadata.?ownerReferences.orValue([]).exists(r,"
                        " r.uid == variables.owner.uid && r.?controller.orValue(false))"
                    ),
                    message="The Policy Controller never takes over an object its ManagedServiceAccount did not control.",
                ),
                # The CRD pins a kubernetesServiceAccount credential's one target and prunes
                # unknown fields.
                k8s.Validation(
                    expression=(
                        "!variables.owned || !variables.credential || ("
                        "has(object.spec.source.kubernetesServiceAccount)"
                        f" && object.spec.source.kubernetesServiceAccount.namespace == '{accounts_namespace}'"
                        " && object.spec.source.kubernetesServiceAccount.name == variables.owner.name)"
                    ),
                    message="A managed account's EgressCredential acts as that account's ServiceAccount.",
                ),
                k8s.Validation(
                    expression=(
                        "!variables.owned || variables.credential || ("
                        "size(object.spec.rules) == 1"
                        " && object.spec.rules[0].all(k, k in ['type', 'hosts', 'clusterInternal', 'credentialRef'])"
                        " && object.spec.rules[0].type == 'egress'"
                        f" && object.spec.rules[0].hosts == ['{egress.KUBERNETES_HOST}']"
                        " && object.spec.rules[0].clusterInternal == true"
                        " && object.spec.rules[0].credentialRef.name == object.metadata.name)"
                    ),
                    message=(
                        "A managed account's PolicySet is one egress rule to the local API server presenting"
                        " the account's own credential."
                    ),
                ),
            ],
        ),
    )
    k8s.KubeValidatingAdmissionPolicyBinding(
        scope,
        "managed-account-use-admission-binding",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(policy_name=name, validation_actions=["Deny"]),
    )
