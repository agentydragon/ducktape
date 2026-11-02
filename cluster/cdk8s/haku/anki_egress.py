"""AnkiWeb sync egress for haku-anki, which runs behind an Agentplane egress sidecar as the `haku-ui` account.

haku-anki talks to exactly one external service, AnkiWeb (`sync.ankiweb.net`, and the `syncNN.ankiweb.net`
shard hosts it redirects accounts to). It reaches it through an `agentplane-egress-sidecar` instead of the
namespace-wide `haku-egress-proxy`, so the namespace's Kyverno proxy injection (`kyverno/proxy_injection.py`)
and the `*.ankiweb.net` fence entry (`egress_fences.py`) can be retired.

There is deliberately no account of its own. haku-sandbox is one trust domain (`haku/namespace.py`), and the
sidecar attachment already exists for the `haku-ui` account (`ui_egress.py`: its ServiceAccount, the gateway
ingress rule), so this module adds only the `ankiweb-sync` PolicySet, which `actions_staging_policies.py`
puts on the `haku-ui` binding. The cost is that the two workloads share a grant set: the haku-ui Pod can now
reach AnkiWeb and the haku-anki Pod can read Home Assistant and Grocy and submit Action requests. Split the
account if either workload ever holds something the other must not.

The rule presents no credential. AnkiWeb login sends the account's own username and password in a
compressed request body, which a placeholder substitution cannot reach, so the real AnkiWeb credentials
stay in the Pod (the `ankiweb-credentials` Secret, as before). This moves the route, not the secret.

Not within `claude-ai`'s grants (it cannot reach AnkiWeb), unlike the rest of haku-ui's. That is deliberate:
Haku already reaches `*.ankiweb.net` through `haku-egress-proxy`'s FQDN fence, so this is the same reach.

The Pod (haku-state `k8s/haku-anki`) carries the same attachment as haku-ui and, because the Kyverno injection
skips a container that sets a proxy, its OWN `HTTPS_PROXY`/`SSL_CERT_FILE` (rslib's reqwest honours both; git to
in-cluster Forgejo uses libgit2, which ignores them).
"""

from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.providers.agentplane.policy_set import Rule

POLICY = "ankiweb-sync"
# `*.` matches subdomains and never the apex (agentplane/egress/SPEC.md); sync and its shards are subdomains.
ANKIWEB_HOSTS = ("*.ankiweb.net",)


def add_policy_set(scope: Construct, *, namespace: str) -> None:
    """The AnkiWeb egress PolicySet, in the Agentplane namespace `namespace`."""
    add_egress_policy(
        Construct(scope, "ankiweb-sync-egress"),
        "policy",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        rules=[Rule.egress(hosts=ANKIWEB_HOSTS)],
    )
