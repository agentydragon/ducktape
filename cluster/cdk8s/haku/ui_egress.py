"""haku-ui's attachment to agentplane-staging, as its own caller.

haku-ui is Haku-authored (haku-state `k8s/haku-ui`, applied by `haku-state-workloads` into
haku-sandbox), so the identity and everything it may reach live here, where Haku cannot write:

- the `haku-ui` ServiceAccount (`haku/rbac.py`), labelled as an Action caller;
- an egress policy for the one Action Service route it uses, bound with the Home Assistant and
  Grocy SF read sets (`actions_staging_policies.py`), so its reads are GETs through the proxy and
  its `<tool-call>` buttons are Action requests that wait for the operator like any other write;
- the gateway ingress rule below.

Invariant: haku-ui's grants stay a subset of `claude-ai`'s. `haku-sandbox-admin` lets Haku create
Pods that run as any ServiceAccount in haku-sandbox and exec into haku-ui's, so whatever is bound
to `haku-ui` is Haku's too; Haku already holds `claude-ai`'s grants, which makes that harmless.
It is deliberately not `claude-ai` itself: that binding auto-approves sandbox create and exec.
`test_haku_ui_egress` pins it.

What the Pod (haku-state) must carry, as public-coder's OpenClaw does (`public_coder/egress.py`):
run as this ServiceAccount; an `agentplane-egress-sidecar` container with settings naming `GATEWAY`
and a projected token (audience `WORKLOAD_TOKEN_AUDIENCE`); the `agentplane-egress-ca` ConfigMap
(published into this namespace by the egress Bundle) as its trust store; and
`forgejo-images-creds` as an imagePullSecret, because the sidecar is a private ducktape-ci image the
`haku` registry user cannot read. Credentials in the Pod are placeholders.

Gotcha (2026-11-01): `kyverno/proxy_injection.py` APPENDS `HTTP(S)_PROXY`, `NO_PROXY` and
`SSL_CERT_FILE` to every container in haku-sandbox, and the last duplicate env entry wins. Its
`NO_PROXY` covers `.svc.cluster.local` and `.allegedly.works`, so a client that takes its proxy from
the environment skips the sidecar and sends the placeholder to the destination unsubstituted (Home
Assistant answered 401). haku-ui therefore names the proxy and CA in code (haku-state
`ui/backend/agentplane.py`, `Egress`).
"""

from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.agentplane import actions, egress
from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.providers.agentplane.policy_set import Rule
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy

NAMESPACE = "haku-sandbox"
SERVICE_ACCOUNT = "haku-ui"
POLICY = "haku-ui"
GATEWAY = egress.proxy("agentplane-staging")


def add_gateway_resources(scope: Construct, *, namespace: str) -> None:
    """The egress policy and the gateway ingress rule, in the Agentplane namespace `namespace`.

    Home Assistant and Grocy reads are not here: their egress rules ride in the Action PolicySets
    that approve the same system's reads, so haku-ui's PolicyBinding names those sets."""
    scope = Construct(scope, "haku-ui-egress")
    add_egress_policy(
        scope,
        "policy",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        rules=[
            # Submit a request and read its receipt; not discovery, the MCP edge or the operator routes.
            Rule.egress(
                hosts=[actions.service(namespace).fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.POST],
                paths=["/v1/action-requests", "/v1/action-requests/**"],
                credential_ref=PolicySetSpecRulesCredentialRef(name="agentplane-workload"),
            )
        ],
    )
    allow_gateway_ingress(scope, namespace=namespace, name=POLICY, service_account=SERVICE_ACCOUNT)


def allow_gateway_ingress(scope: Construct, *, namespace: str, name: str, service_account: str) -> None:
    """Admit pods running as `service_account` in haku-sandbox to the egress gateway, from the Agentplane
    namespace `namespace`. Shared by every haku-sandbox workload that carries an egress sidecar."""
    NetworkPolicy(
        scope,
        "gateway-network",
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        endpoint_selector=GATEWAY.pods.selector,
        ingress=[
            IngressRule.from_endpoints(
                {"k8s:io.kubernetes.pod.namespace": NAMESPACE, "io.cilium.k8s.policy.serviceaccount": service_account},
                ports=[GATEWAY.pod_port],
            )
        ],
    )
