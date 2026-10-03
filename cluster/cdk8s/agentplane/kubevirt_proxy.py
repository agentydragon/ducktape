"""Opt-in launcher relay admission for namespaces dedicated to Agentplane VMs.

This construct is deliberately not installed by the staging/testing charts until the
platform prototype has passed. The guest never receives a Kubernetes token volume.
"""

import json

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicy,
    ClusterPolicySpec,
    ClusterPolicySpecFailurePolicy,
    ClusterPolicySpecRules,
    ClusterPolicySpecRulesContext,
    ClusterPolicySpecRulesContextApiCall,
    ClusterPolicySpecRulesContextVariable,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesMatchAnyResourcesOperations,
    ClusterPolicySpecRulesMatchAnyResourcesSelector,
    ClusterPolicySpecRulesMutate,
    ClusterPolicySpecValidationFailureAction,
)

from cluster.cdk8s.providers.kyverno.cluster_policy import Validate, match_resources

MANAGED_LABEL = "agentplane.allegedly.works/managed"
SERVICE_ACCOUNT_ANNOTATION = "agentplane.allegedly.works/launcher-service-account"
TOKEN_VOLUME = "agentplane-egress-token"
RELAY_CONTAINER = "egress-sidecar"
TOKEN_DIRECTORY = "/var/run/agentplane-egress"
CONTROLLER_USERNAME = "system:serviceaccount:kubevirt:kubevirt-controller"


def _owner(kind: str) -> str:
    return f"metadata.ownerReferences[?kind=='{kind}' && apiVersion=='kubevirt.io/v1' && controller==`true`] | [0]"


def _context() -> list[ClusterPolicySpecRulesContext]:
    return [
        ClusterPolicySpecRulesContext(
            name="vmiOwner",
            variable=ClusterPolicySpecRulesContextVariable(
                jmes_path=f"request.object.{_owner('VirtualMachineInstance')}", default={}
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="vmi",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path="/apis/kubevirt.io/v1/namespaces/{{request.namespace}}/virtualmachineinstances/{{vmiOwner.name || 'missing'}}"
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="vmOwner",
            variable=ClusterPolicySpecRulesContextVariable(jmes_path=f"vmi.{_owner('VirtualMachine')}", default={}),
        ),
        ClusterPolicySpecRulesContext(
            name="vm",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path="/apis/kubevirt.io/v1/namespaces/{{request.namespace}}/virtualmachines/{{vmOwner.name || 'missing'}}"
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="account",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path='/api/v1/namespaces/{{request.namespace}}/serviceaccounts/{{vm.metadata.annotations."'
                + SERVICE_ACCOUNT_ANNOTATION
                + "\" || 'missing'}}"
            ),
        ),
    ]


def _mismatch(key: str, value: object) -> dict[str, object]:
    return {"key": "{{ " + key + " }}", "operator": "NotEquals", "value": value}


class KubeVirtProxyPolicy(ClusterPolicy):
    """Authenticate launcher creation and mount rotating identity only in its relay."""

    def __init__(
        self, scope: Construct, id: str, *, namespace: str, image: str, proxy_host: str, approved_templates: list[str]
    ) -> None:
        match = match_resources(
            ClusterPolicySpecRulesMatchAnyResources(
                kinds=["Pod"],
                namespaces=[namespace],
                operations=[ClusterPolicySpecRulesMatchAnyResourcesOperations.CREATE],
                selector=ClusterPolicySpecRulesMatchAnyResourcesSelector(match_labels={"kubevirt.io": "virt-launcher"}),
            )
        )
        updates = match_resources(
            ClusterPolicySpecRulesMatchAnyResources(
                kinds=["Pod", "Pod/ephemeralcontainers"],
                namespaces=[namespace],
                operations=[ClusterPolicySpecRulesMatchAnyResourcesOperations.UPDATE],
            )
        )
        identity_conditions = [
            {
                "key": "{{vm.metadata.annotations.\"agentplane.allegedly.works/vm-template\" || ''}}",
                "operator": "AnyNotIn",
                "value": approved_templates,
            },
            _mismatch("request.userInfo.username", CONTROLLER_USERNAME),
            _mismatch("vmiOwner.uid", "{{vmi.metadata.uid}}"),
            _mismatch("vmOwner.uid", "{{vm.metadata.uid}}"),
            _mismatch(f"vm.metadata.labels.\"{MANAGED_LABEL}\" || ''", "true"),
            _mismatch(
                "account.metadata.ownerReferences[?kind=='VirtualMachine' && apiVersion=='kubevirt.io/v1' && controller==`true`].uid | [0] || ''",
                "{{vm.metadata.uid}}",
            ),
            _mismatch("length(vmi.spec.volumes[?serviceAccount] || `[]`)", 0),
        ]
        env = {
            "PROXY_HOST": proxy_host,
            "PROXY_PORT": "8888",
            "LISTEN_HOST": "0.0.0.0",
            "LISTEN_PORT": "3128",
            "READINESS_HOST": "0.0.0.0",
            "READINESS_PORT": "3129",
            "TOKEN_FILE": f"{TOKEN_DIRECTORY}/token",
            "AUDIENCE_TOKEN_FILES": json.dumps({"https://localhost:7445": f"{TOKEN_DIRECTORY}/kubernetes-token"}),
        }
        relay = {
            "name": RELAY_CONTAINER,
            "image": image,
            "env": [{"name": f"AGENTPLANE_EGRESS_SIDECAR_{key}", "value": value} for key, value in env.items()],
            "securityContext": {
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
            "readinessProbe": {"httpGet": {"path": "/readyz", "port": 3129}, "periodSeconds": 2, "timeoutSeconds": 1},
            "volumeMounts": [{"name": TOKEN_VOLUME, "mountPath": TOKEN_DIRECTORY, "readOnly": True}],
        }
        patch = {
            "spec": {
                "serviceAccountName": "{{account.metadata.name}}",
                "automountServiceAccountToken": False,
                "imagePullSecrets": [{"name": "forgejo-images-creds"}],
                "containers": [relay],
                "volumes": [
                    {
                        "name": TOKEN_VOLUME,
                        "projected": {
                            "defaultMode": 420,
                            "sources": [
                                {
                                    "serviceAccountToken": {
                                        "audience": "agentplane-egress",
                                        "expirationSeconds": 600,
                                        "path": "token",
                                    }
                                },
                                {
                                    "serviceAccountToken": {
                                        "audience": "https://localhost:7445",
                                        "expirationSeconds": 600,
                                        "path": "kubernetes-token",
                                    }
                                },
                            ],
                        },
                    }
                ],
            }
        }
        super().__init__(
            scope,
            id,
            metadata=ApiObjectMetadata(
                name=f"{namespace}-launcher-relay", annotations={"pod-policies.kyverno.io/autogen-controllers": "none"}
            ),
            spec=ClusterPolicySpec(
                background=False,
                failure_policy=ClusterPolicySpecFailurePolicy.FAIL,
                validation_failure_action=ClusterPolicySpecValidationFailureAction.ENFORCE,
                rules=[
                    ClusterPolicySpecRules(
                        name="protect-token-on-update",
                        match=updates,
                        validate=Validate.deny(
                            message="Only the launcher relay may mount its projected token.",
                            conditions={
                                "any": [
                                    _mismatch(
                                        f"length((request.object.spec.ephemeralContainers || `[]`)[].volumeMounts[] | [?name=='{TOKEN_VOLUME}'])",
                                        0,
                                    ),
                                    _mismatch(
                                        f"length(request.object.spec.containers[?name!='{RELAY_CONTAINER}'].volumeMounts[] | [?name=='{TOKEN_VOLUME}'])",
                                        0,
                                    ),
                                    _mismatch(
                                        f"length((request.object.spec.initContainers || `[]`)[].volumeMounts[] | [?name=='{TOKEN_VOLUME}'])",
                                        0,
                                    ),
                                ]
                            },
                        ),
                    ),
                    ClusterPolicySpecRules(
                        name="authenticate-launcher",
                        match=match,
                        context=_context(),
                        validate=Validate.deny(
                            message="Launcher must be created by KubeVirt for a live managed VM and its owned account.",
                            conditions={"any": identity_conditions},
                        ),
                    ),
                    ClusterPolicySpecRules(
                        name="inject-relay",
                        match=match,
                        context=_context(),
                        mutate=ClusterPolicySpecRulesMutate(patch_strategic_merge=patch),
                    ),
                    ClusterPolicySpecRules(
                        name="validate-relay",
                        match=match,
                        context=_context(),
                        validate=Validate.deny(
                            message="Launcher relay identity must stay outside compute and init containers.",
                            conditions={
                                "any": [
                                    _mismatch("request.object.spec.serviceAccountName", "{{account.metadata.name}}"),
                                    _mismatch("request.object.spec.automountServiceAccountToken", False),
                                    _mismatch(f"length(request.object.spec.containers[?name=='{RELAY_CONTAINER}'])", 1),
                                    _mismatch(
                                        f"length(request.object.spec.containers[?name!='{RELAY_CONTAINER}'].volumeMounts[] | [?name=='{TOKEN_VOLUME}'])",
                                        0,
                                    ),
                                    _mismatch(
                                        f"length((request.object.spec.initContainers || `[]`)[].volumeMounts[] | [?name=='{TOKEN_VOLUME}'])",
                                        0,
                                    ),
                                ]
                            },
                        ),
                    ),
                ],
            ),
        )
