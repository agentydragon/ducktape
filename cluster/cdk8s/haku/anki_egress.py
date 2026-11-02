"""haku-anki's attachment to agentplane-staging egress, for AnkiWeb sync.

haku-anki talks to exactly one external service, AnkiWeb (`sync.ankiweb.net`, and the `syncNN.ankiweb.net`
shard hosts it redirects accounts to). It reaches it through an `agentplane-egress-sidecar` instead of the
namespace-wide `haku-egress-proxy`: the `haku-anki` ServiceAccount (`haku/rbac.py`) is bound to the
`haku-anki` egress policy below, so the grant is per workload and visible in one place.

The rule presents no credential. AnkiWeb login sends the account's own username and password in a
compressed request body, which a placeholder substitution cannot reach, so the real AnkiWeb credentials
stay in the Pod (the `ankiweb-credentials` Secret, as before). This moves the *route*, not the secret.

Not a subset of `claude-ai`'s grants (it cannot reach AnkiWeb). That is deliberate: Haku already reaches
`*.ankiweb.net` today through `haku-egress-proxy`'s FQDN fence (`egress_fences.py`), so this is the same
reach on a narrower identity. Once that fence entry and the Kyverno injection (`kyverno/proxy_injection.py`)
are retired, this is the only way out.

The Pod (haku-state `k8s/haku-anki`) must carry the same attachment as haku-ui (`haku/ui_egress.py`): run
as this ServiceAccount, an `agentplane-egress-sidecar` with a projected token and settings naming
`ui_egress.GATEWAY`, the `agentplane-egress-ca` bundle as its trust store, `forgejo-images-creds` to pull the
private sidecar image, and its OWN `HTTPS_PROXY`/`SSL_CERT_FILE` (the Kyverno injection skips a container
that sets a proxy). rslib's reqwest honours both; git to in-cluster Forgejo uses libgit2, which ignores them.
"""

from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.haku import ui_egress
from cluster.cdk8s.providers.agentplane.policy_set import Rule

NAMESPACE = ui_egress.NAMESPACE
SERVICE_ACCOUNT = "haku-anki"
POLICY = "haku-anki"
# `*.` matches subdomains and never the apex (agentplane/egress/SPEC.md); sync and its shards are subdomains.
ANKIWEB_HOSTS = ("*.ankiweb.net",)


def add_gateway_resources(scope: Construct, *, namespace: str) -> None:
    """The egress policy and the gateway ingress rule, in the Agentplane namespace `namespace`."""
    scope = Construct(scope, "haku-anki-egress")
    add_egress_policy(
        scope,
        "policy",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        rules=[Rule.egress(hosts=ANKIWEB_HOSTS)],
    )
    ui_egress.allow_gateway_ingress(scope, namespace=namespace, name=POLICY, service_account=SERVICE_ACCOUNT)
